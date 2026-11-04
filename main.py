"""Redactor seguro de PDFs.

Elimina de forma irreversible el texto sensible de los archivos PDF
mediante anotaciones de redaccion (``add_redact_annot`` +
``apply_redactions``), en lugar de cubrirlo con una capa de imagen.

Uso grafico (por defecto)::

    python main.py

Uso por linea de comandos::

    python main.py --input-dir ./pdfs --output-dir ./salida --auto all
    python main.py --input-dir ./pdfs --output-dir ./salida --terms "Juan Perez" --terms "juan@mail.com"
"""

from __future__ import annotations

import argparse
import logging
import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf
except ImportError:  # Compatibilidad con alias antiguo.
    import fitz as pymupdf  # type: ignore[no-redef]

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

__version__ = "2.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pdf_redactor")

OUTPUT_PREFIX = "redactado_"
REDACT_FILL = (0, 0, 0)  # Negro: maximo contraste, sin informacion residual.
RECT_PADDING = 2.0  # Puntos extra alrededor de cada coincidencia.


# ---------------------------------------------------------------------------
# Patrones de datos sensibles
# ---------------------------------------------------------------------------

PATTERNS: dict[str, tuple[str, str]] = {
    # nombre_interno: (etiqueta visible, expresion regular).
    "emails": ("Emails", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phones": (
        "Telefonos",
        r"(?:\+?\d{1,3}[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{3,4}",
    ),
    "dni": ("DNI/NIF/NIE", r"(?:\d{8}[A-Z]|[XYZ]\d{7}[A-Z])"),
    "credit": ("Tarjetas de credito", r"(?:\d[\s\-]?){13,19}"),
    "iban": (
        "IBAN",
        r"ES\d{2}(?:[\s.\-]?\d{4}){5}|[A-Z]{2}\d{2}(?:[\s.\-]?\d{4})+",
    ),
    "dates": ("Fechas", r"\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"),
}


def luhn_valid(number: str) -> bool:
    """Valida un numero de tarjeta con el algoritmo de Luhn.

    Evita falsos positivos del patron generico de tarjetas.
    """
    digits = [int(ch) for ch in number if ch.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def compile_patterns(names: list[str]) -> dict[str, re.Pattern[str]]:
    """Compila los patrones automatizados solicitados."""
    compiled: dict[str, re.Pattern[str]] = {}
    for name in names:
        if name not in PATTERNS:
            raise ValueError(f"Tipo de dato desconocido: {name!r}")
        _, regex = PATTERNS[name]
        compiled[name] = re.compile(regex, re.IGNORECASE)
    return compiled


# ---------------------------------------------------------------------------
# Nucleo de redaccion (sin dependencias de interfaz)
# ---------------------------------------------------------------------------


@dataclass
class RedactionConfig:
    """Opciones de una tarea de redaccion."""

    manual_terms: list[str] = field(default_factory=list)
    auto_patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)
    rect_padding: float = RECT_PADDING
    fill_color: tuple[float, float, float] = REDACT_FILL
    sanitize_metadata: bool = True
    verify: bool = True


@dataclass
class RedactionResult:
    """Resultado de redactar un archivo."""

    input_path: Path
    output_path: Path
    redactions: int
    verified: bool | None  # None = verificacion desactivada.
    leftovers: list[str] = field(default_factory=list)


class PDFRedactor:
    """Aplica redaccion real (eliminacion de contenido) sobre PDFs."""

    def __init__(self, config: RedactionConfig) -> None:
        if not config.manual_terms and not config.auto_patterns:
            raise ValueError("Config sin criterios: indica terminos o patrones.")
        self.config = config

    # -- API publica ------------------------------------------------------
    def redact_file(self, input_path: Path, output_path: Path) -> RedactionResult:
        """Redacta un PDF y guarda el resultado saneado en ``output_path``."""
        doc = pymupdf.open(input_path)
        try:
            total = 0
            for page in doc:
                rects = self._collect_rects(page)
                rects = merge_rects(rects)
                if not rects:
                    continue
                for rect in rects:
                    page.add_redact_annot(rect, fill=self.config.fill_color)
                # text=0 elimina el texto; images=2 borra los pixeles
                # solapados; graphics=1 quita vectoriales contenidos.
                page.apply_redactions(images=2, graphics=1, text=0)
                total += len(rects)

            if self.config.sanitize_metadata:
                sanitize_document(doc)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            # garbage + clean + deflate: compacta y elimina objetos huerfanos
            # para que el contenido redactado no quede recuperable.
            doc.save(output_path, garbage=4, deflate=True, clean=True)
        finally:
            doc.close()

        verified: bool | None = None
        leftovers: list[str] = []
        if self.config.verify:
            leftovers = self.verify_file(output_path)
            verified = not leftovers
        return RedactionResult(
            input_path=input_path,
            output_path=output_path,
            redactions=total,
            verified=verified,
            leftovers=leftovers,
        )

    def verify_file(self, pdf_path: Path) -> list[str]:
        """Comprueba que no queda rastro extraible de los criterios.

        Devuelve la lista de terminos/patrones aun detectables (vacia = OK).
        """
        leftovers: list[str] = []
        doc = pymupdf.open(pdf_path)
        try:
            full_text = "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
        lowered = full_text.lower()

        for term in self.config.manual_terms:
            if term.strip() and term.strip().lower() in lowered:
                leftovers.append(term.strip())

        for name, pattern in self.config.auto_patterns.items():
            match = pattern.search(full_text)
            if match and not (
                name == "credit" and not luhn_valid(match.group())
            ):
                leftovers.append(f"<patron:{name}> {match.group()}")
        return leftovers

    # -- Localizacion de coincidencias ------------------------------------
    def _collect_rects(self, page: pymupdf.Page) -> list[pymupdf.Rect]:
        rects: list[pymupdf.Rect] = []
        rects.extend(self._manual_rects(page))
        if self.config.auto_patterns:
            rects.extend(self._automatic_rects(page))
        return rects

    def _manual_rects(self, page: pymupdf.Page) -> list[pymupdf.Rect]:
        """Rectangulos de los terminos literales (insensible a mayusculas)."""
        rects: list[pymupdf.Rect] = []
        for term in self.config.manual_terms:
            term = term.strip()
            if not term:
                continue
            # search_for localiza el texto tal como esta compuesto en pagina.
            for hit in page.search_for(term):
                rects.append(expand_rect(hit, page.rect, self.config.rect_padding))
            # Respaldo insensible a mayusculas sobre el mapa de palabras,
            # por si search_for no cubre alguna variante de caja.
            pattern = re.compile(re.escape(term), re.IGNORECASE)
            rects.extend(self._regex_rects(page, pattern, validator=None))
        return rects

    def _automatic_rects(self, page: pymupdf.Page) -> list[pymupdf.Rect]:
        rects: list[pymupdf.Rect] = []
        for name, pattern in self.config.auto_patterns.items():
            validator = luhn_valid if name == "credit" else None
            rects.extend(self._regex_rects(page, pattern, validator=validator))
        return rects

    def _regex_rects(
        self,
        page: pymupdf.Page,
        pattern: re.Pattern[str],
        validator: object = None,
    ) -> list[pymupdf.Rect]:
        """Convierte coincidencias regex en rectangulos de pagina.

        Reconstruye el texto uniendo las palabras de la pagina para mapear
        cada coincidencia (incluso las que abarcan varias palabras, como un
        IBAN con espacios) a la union de sus rectangulos.
        """
        words = page.get_text("words")
        if not words:
            return []
        tokens = [w[4] for w in words]
        offsets: list[int] = []
        pos = 0
        for token in tokens:
            offsets.append(pos)
            pos += len(token) + 1
        full = " ".join(tokens)

        rects: list[pymupdf.Rect] = []
        for match in pattern.finditer(full):
            start, end = match.span()
            if validator is not None and not validator(match.group()):  # type: ignore[operator]
                continue
            hit_words = [
                words[i]
                for i, off in enumerate(offsets)
                if off < end and off + len(tokens[i]) > start
            ]
            if not hit_words:
                continue
            x0 = min(w[0] for w in hit_words)
            y0 = min(w[1] for w in hit_words)
            x1 = max(w[2] for w in hit_words)
            y1 = max(w[3] for w in hit_words)
            rects.append(
                expand_rect(pymupdf.Rect(x0, y0, x1, y1), page.rect, self.config.rect_padding)
            )
        return rects


def expand_rect(
    rect: pymupdf.Rect, page_rect: pymupdf.Rect, padding: float
) -> pymupdf.Rect:
    """Expande un rectangulo y lo recorta a los limites de la pagina."""
    expanded = pymupdf.Rect(
        rect.x0 - padding, rect.y0 - padding, rect.x1 + padding, rect.y1 + padding
    )
    return expanded & page_rect


def merge_rects(rects: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Fusiona rectangulos solapados para evitar anotaciones duplicadas."""
    merged: list[pymupdf.Rect] = []
    for rect in rects:
        if rect.is_empty or rect.is_infinite:
            continue
        for i, existing in enumerate(merged):
            if rect.intersects(existing):
                merged[i] = rect | existing
                break
        else:
            merged.append(rect)
    return merged


def sanitize_document(doc: pymupdf.Document) -> None:
    """Elimina metadatos y adjuntos que podrian filtrar informacion."""
    doc.set_metadata({})
    try:
        doc.del_xml_metadata()
    except (AttributeError, RuntimeError):
        pass
    try:
        for name in doc.embfile_names():
            doc.embfile_del(name)
    except RuntimeError:
        pass


def list_pdfs(directory: Path) -> list[Path]:
    """Lista los PDF de un directorio (insensible a la extension)."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"
    )


# ---------------------------------------------------------------------------
# Interfaz grafica
# ---------------------------------------------------------------------------


@dataclass
class RedactionJob:
    """Parametros capturados de la interfaz antes de lanzar el hilo."""

    input_dir: Path
    output_dir: Path
    files: list[Path]
    mode: str  # "manual" | "automatic".
    terms: list[str] = field(default_factory=list)
    pattern_names: list[str] = field(default_factory=list)
    verify: bool = True
    sanitize: bool = True


class PDFRedactorApp:
    """Interfaz Tkinter sobre :class:`PDFRedactor` (hilo seguro)."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"Redactor seguro de PDFs v{__version__}")
        self.root.geometry("920x740")
        self.root.minsize(760, 600)

        self.files: list[Path] = []
        self.queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.working = False

        self._build_widgets()
        self._toggle_mode()
        self._poll_queue()

    # -- Construccion -----------------------------------------------------
    def _build_widgets(self) -> None:
        dir_frame = ttk.LabelFrame(self.root, text="Carpetas", padding=10)
        dir_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(dir_frame, text="Carpeta de PDFs:").grid(row=0, column=0, sticky=tk.W)
        self.dir_entry = ttk.Entry(dir_frame, width=62)
        self.dir_entry.grid(row=0, column=1, padx=5, pady=4, sticky=tk.EW)
        ttk.Button(dir_frame, text="Examinar", command=self._select_input).grid(
            row=0, column=2
        )
        ttk.Label(dir_frame, text="Carpeta de salida:").grid(row=1, column=0, sticky=tk.W)
        self.output_entry = ttk.Entry(dir_frame, width=62)
        self.output_entry.grid(row=1, column=1, padx=5, pady=4, sticky=tk.EW)
        ttk.Button(dir_frame, text="Examinar", command=self._select_output).grid(
            row=1, column=2
        )
        dir_frame.columnconfigure(1, weight=1)

        mode_frame = ttk.LabelFrame(self.root, text="Modo de redaccion", padding=10)
        mode_frame.pack(fill=tk.X, padx=10, pady=5)
        self.mode_var = tk.StringVar(value="manual")
        ttk.Radiobutton(
            mode_frame,
            text="Manual (terminos exactos, uno por linea)",
            variable=self.mode_var,
            value="manual",
            command=self._toggle_mode,
        ).pack(anchor=tk.W)
        ttk.Radiobutton(
            mode_frame,
            text="Automatico (detectar datos sensibles)",
            variable=self.mode_var,
            value="automatic",
            command=self._toggle_mode,
        ).pack(anchor=tk.W)

        self.manual_frame = ttk.LabelFrame(
            self.root, text="Terminos a eliminar", padding=10
        )
        self.manual_text = scrolledtext.ScrolledText(self.manual_frame, height=6)
        self.manual_text.pack(fill=tk.BOTH, expand=True)

        self.auto_frame = ttk.LabelFrame(
            self.root, text="Tipos de datos a detectar", padding=10
        )
        self.pattern_vars: dict[str, tk.BooleanVar] = {}
        defaults = {"emails": True, "phones": True, "dni": True,
                    "credit": True, "iban": True, "dates": False}
        for col, (name, (label, _)) in enumerate(PATTERNS.items()):
            var = tk.BooleanVar(value=defaults.get(name, True))
            self.pattern_vars[name] = var
            ttk.Checkbutton(self.auto_frame, text=label, variable=var).grid(
                row=col // 3, column=col % 3, sticky=tk.W, padx=8, pady=3
            )

        opts_frame = ttk.LabelFrame(self.root, text="Seguridad", padding=10)
        opts_frame.pack(fill=tk.X, padx=10, pady=5)
        self.verify_var = tk.BooleanVar(value=True)
        self.sanitize_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opts_frame, text="Verificar eliminacion tras redactar",
            variable=self.verify_var,
        ).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(
            opts_frame, text="Limpiar metadatos y adjuntos",
            variable=self.sanitize_var,
        ).pack(side=tk.LEFT, padx=8)

        control = ttk.Frame(self.root, padding=10)
        control.pack(fill=tk.X, padx=10)
        self.load_btn = ttk.Button(control, text="Cargar PDFs", command=self._load_pdfs)
        self.load_btn.pack(side=tk.LEFT, padx=5)
        self.run_btn = ttk.Button(
            control, text="Redactar PDFs", command=self._start_job
        )
        self.run_btn.pack(side=tk.LEFT, padx=5)
        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(control, textvariable=self.status_var).pack(side=tk.LEFT, padx=12)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=10, pady=5)

        log_frame = ttk.LabelFrame(self.root, text="Registro", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=10, state=tk.DISABLED
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # -- Interaccion ------------------------------------------------------
    def _toggle_mode(self) -> None:
        if self.mode_var.get() == "manual":
            self.auto_frame.pack_forget()
            self.manual_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        else:
            self.manual_frame.pack_forget()
            self.auto_frame.pack(fill=tk.X, padx=10, pady=5)

    def _select_input(self) -> None:
        directory = filedialog.askdirectory(title="Carpeta con PDFs")
        if directory:
            self.dir_entry.delete(0, tk.END)
            self.dir_entry.insert(0, directory)

    def _select_output(self) -> None:
        directory = filedialog.askdirectory(title="Carpeta de salida")
        if directory:
            self.output_entry.delete(0, tk.END)
            self.output_entry.insert(0, directory)

    def _load_pdfs(self) -> None:
        directory = Path(self.dir_entry.get().strip())
        self.files = list_pdfs(directory)
        if not directory.is_dir():
            messagebox.showerror("Error", "Selecciona una carpeta de entrada valida.")
            return
        self._log(f"Se encontraron {len(self.files)} archivos PDF en {directory}")
        self.status_var.set(f"{len(self.files)} PDF cargados.")
        if not self.files:
            messagebox.showinfo("Sin PDFs", "No hay archivos .pdf en esa carpeta.")

    def _start_job(self) -> None:
        if self.working:
            return
        if not self.files:
            messagebox.showerror("Error", "Carga los PDFs primero.")
            return
        output_dir = Path(self.output_entry.get().strip())
        if not str(output_dir):
            messagebox.showerror("Error", "Selecciona una carpeta de salida.")
            return

        mode = self.mode_var.get()
        terms: list[str] = []
        pattern_names: list[str] = []
        if mode == "manual":
            terms = [
                line.strip()
                for line in self.manual_text.get("1.0", tk.END).splitlines()
                if line.strip()
            ]
            if not terms:
                messagebox.showerror("Error", "Ingresa al menos un termino.")
                return
        else:
            pattern_names = [
                name for name, var in self.pattern_vars.items() if var.get()
            ]
            if not pattern_names:
                messagebox.showerror("Error", "Marca al menos un tipo de dato.")
                return

        # Capturar todo en el hilo principal: Tkinter no es hilo seguro.
        job = RedactionJob(
            input_dir=Path(self.dir_entry.get().strip()),
            output_dir=output_dir,
            files=list(self.files),
            mode=mode,
            terms=terms,
            pattern_names=pattern_names,
            verify=self.verify_var.get(),
            sanitize=self.sanitize_var.get(),
        )
        self.working = True
        self.load_btn.config(state=tk.DISABLED)
        self.run_btn.config(state=tk.DISABLED)
        self.progress["value"] = 0
        self.progress["maximum"] = len(job.files)
        self.status_var.set("Redactando...")
        threading.Thread(target=self._run_job, args=(job,), daemon=True).start()

    # -- Proceso en segundo plano (sin tocar Tkinter) ---------------------
    def _run_job(self, job: RedactionJob) -> None:
        try:
            config = RedactionConfig(
                manual_terms=job.terms if job.mode == "manual" else [],
                auto_patterns=(
                    compile_patterns(job.pattern_names)
                    if job.mode == "automatic"
                    else {}
                ),
                sanitize_metadata=job.sanitize,
                verify=job.verify,
            )
            redactor = PDFRedactor(config)
            job.output_dir.mkdir(parents=True, exist_ok=True)
            ok_count = 0
            for i, pdf_path in enumerate(job.files):
                try:
                    output_path = job.output_dir / f"{OUTPUT_PREFIX}{pdf_path.name}"
                    result = redactor.redact_file(pdf_path, output_path)
                    status = self._format_result(result)
                    self.queue.put(("log", status))
                    logger.info(status)
                    ok_count += 1
                except Exception as exc:  # Un archivo no detiene el lote.
                    msg = f"Error en {pdf_path.name}: {exc}"
                    self.queue.put(("log", msg))
                    logger.exception(msg)
                finally:
                    self.queue.put(("progress", i + 1))
            self.queue.put(("done", f"Completado: {ok_count}/{len(job.files)} archivos."))
        except Exception as exc:
            self.queue.put(("done", f"Error general: {exc}"))

    @staticmethod
    def _format_result(result: RedactionResult) -> str:
        base = (f"{result.input_path.name}: {result.redactions} "
                f"fragmentos eliminados -> {result.output_path.name}")
        if result.verified is None:
            return f"[OK] {base} (sin verificacion)"
        if result.verified:
            return f"[OK] {base} (verificado: sin rastros)"
        return f"[AVISO] {base} (quedan rastros: {', '.join(result.leftovers)})"

    # -- Bomba de eventos hilo principal ----------------------------------
    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "progress":
                    self.progress["value"] = int(payload)
                    self.status_var.set(
                        f"Procesados {int(payload)}/{int(self.progress['maximum'])}..."
                    )
                elif kind == "done":
                    self.working = False
                    self.load_btn.config(state=tk.NORMAL)
                    self.run_btn.config(state=tk.NORMAL)
                    self.status_var.set("Listo.")
                    self._log(str(payload))
                    messagebox.showinfo("Completado", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _log(self, message: str) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)


# ---------------------------------------------------------------------------
# Linea de comandos
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Redaccion segura de PDFs (eliminacion real del contenido)."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--input-dir", type=Path, help="Carpeta con PDFs de entrada.")
    parser.add_argument("--output-dir", type=Path, help="Carpeta de salida.")
    parser.add_argument(
        "--terms", action="append", default=[],
        help="Termino literal a eliminar. Repetible.",
    )
    parser.add_argument(
        "--terms-file", type=Path,
        help="Archivo con un termino por linea.",
    )
    parser.add_argument(
        "--auto", nargs="*", metavar="TIPO",
        help=f"Deteccion automatica. Tipos: {', '.join(PATTERNS)} o 'all'.",
    )
    parser.add_argument("--no-verify", action="store_true", help="Omite la verificacion.")
    parser.add_argument("--no-sanitize", action="store_true", help="Conserva metadatos.")
    parser.add_argument("--gui", action="store_true", help="Abrir interfaz grafica.")
    return parser.parse_args(argv)


def run_cli(args: argparse.Namespace) -> int:
    if not args.input_dir or not args.output_dir:
        print("Faltan --input-dir y --output-dir (o usa --gui).", file=sys.stderr)
        return 2
    files = list_pdfs(args.input_dir)
    if not files:
        print(f"Sin PDFs en {args.input_dir}", file=sys.stderr)
        return 1

    terms = list(args.terms)
    if args.terms_file:
        terms.extend(
            line.strip()
            for line in args.terms_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    pattern_names: list[str] = []
    if args.auto is not None:
        pattern_names = list(PATTERNS) if "all" in args.auto else list(args.auto)
    if not terms and not pattern_names:
        print("Indica --terms/--terms-file o --auto.", file=sys.stderr)
        return 2

    config = RedactionConfig(
        manual_terms=terms,
        auto_patterns=compile_patterns(pattern_names),
        sanitize_metadata=not args.no_sanitize,
        verify=not args.no_verify,
    )
    redactor = PDFRedactor(config)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for pdf_path in files:
        try:
            result = redactor.redact_file(
                pdf_path, args.output_dir / f"{OUTPUT_PREFIX}{pdf_path.name}"
            )
            print(PDFRedactorApp._format_result(result))
            if result.verified is False:
                failures += 1
        except Exception as exc:
            print(f"[ERROR] {pdf_path.name}: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    wants_cli = bool(args.input_dir or args.output_dir or args.terms
                     or args.terms_file or args.auto is not None)
    if wants_cli and not args.gui:
        return run_cli(args)
    root = tk.Tk()
    PDFRedactorApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
