# frontend/

Fuente TypeScript de la lógica interactiva de la wiki (gráficas Chart.js
hoy; punto de extensión para KaTeX/Material Design Icons a futuro, ver
`src/marc-interactive.ts`).

**Herramienta de la máquina de desarrollo únicamente.** El Python
empaquetado (`runtime/`) no lleva Node, y ningún instalador ni el
`webapp/server.py` compilan nada en tiempo de ejecución. El único archivo
que MARC realmente carga es el `.js` ya compilado en `../vendor/`
(`marc-interactive.js`), commiteado como cualquier otro asset vendorizado
— igual que `chart.min.js`/`mermaid.min.js`, que son librerías de
terceros en vez de código propio.

## Compilar

```sh
cd frontend
npm install   # una vez, o tras cambiar de máquina
npm run build # compila src/marc-interactive.ts -> ../vendor/marc-interactive.js
```

Después de compilar, el archivo cambiado en `vendor/marc-interactive.js`
se commitea junto con el `.ts` fuente — el build real (`mkdocs build`,
disparado por `webapp/server.py`) solo copia lo que ya esté en `vendor/`,
nunca invoca `tsc`.
