# Redactor seguro de PDFs

Se me ocurrio este pequeño script, por que tenia mis certificaods en PDF, pero no estaba seguro
de si exponer a todo internet mis datos mas sensibles que estaban en estos, se me ocurrio 
crear este simple script para ayudarme con eso, con este, los datos sencibles quedan ilegibles!.
para 

Elimina de forma **irreversible** el texto sensible de archivos PDF. No cubre
los datos con una capa ni con desenfoque: los **remueve del documento** con
anotaciones de redacción (`add_redact_annot` + `apply_redactions`), sanea el
archivo al guardar (`garbage` + `clean` + `deflate`) y verifica que no queden
rastros extraíbles.

## Instalación

```bash
pip install -r requirements.txt
```

Solo requiere `pymupdf`. La interfaz usa `tkinter` (incluido en Python).

## Uso gráfico

```bash
python main.py
```

1. Selecciona la carpeta de entrada y la de salida.
2. Elige modo **Manual** (términos exactos) o **Automático** (emails,
   teléfonos, DNI/NIF/NIE, tarjetas, IBAN, fechas).
3. Pulsa **Redactar PDFs**. El registro indica fragmentos eliminados y si la
   verificación encontró rastros.

## Uso por línea de comandos

```bash
# Automático (todos los patrones)
python main.py --input-dir ./pdfs --output-dir ./salida --auto all

# Manual
python main.py --input-dir ./pdfs --output-dir ./salida --terms "Juan Pérez" --terms "juan@mail.com"

# Términos desde archivo + solo emails y DNI
python main.py --input-dir ./pdfs --output-dir ./salida --terms-file terms.txt --auto emails dni
```

Tipos de `--auto`: `emails`, `phones`, `dni`, `credit`, `iban`, `dates`, `all`.

## Seguridad

- El texto redactado se elimina del contenido de la página, no se tapa.
- Las imágenes y gráficos solapados con la zona se borran o recortan.
- Se limpian metadatos y archivos adjuntos (desactivable con `--no-sanitize`).
- Tras cada archivo se reabre el PDF y se comprueba que los términos y
  patrones ya no son extraíbles (desactivable con `--no-verify`).
- Los originales no se modifican; la salida usa el prefijo `redactado_`.

> Nota: si el dato sensible está en una imagen escaneada (no como texto),
> la redacción borra los píxeles de la zona detectada vía texto. Para
> escaneos sin capa de texto se requeriría OCR previo (no incluido).
