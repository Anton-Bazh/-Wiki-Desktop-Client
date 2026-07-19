# Wiki Desktop Client

Prueba de concepto del motor de renderizado (MkDocs + Material) que luego se empaquetará dentro de un cascarón Tauri.

## Diagrama de ejemplo (Mermaid)

```mermaid
flowchart LR
    A[git pull automático] --> B[mkdocs serve en segundo plano]
    B --> C[WebView Tauri]
    C --> D[Usuario navega la wiki]
```

## Bloque de código

```python
def hola():
    return "mkdocs-material funcionando"
```
