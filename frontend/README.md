# frontend/

Fuente TypeScript de la lógica interactiva de la wiki: gráficas Chart.js,
fórmulas KaTeX (`src/marc-interactive.ts`) y diagramas arrastrables con
React Flow (`src/marc-flow.tsx`, bloque ```` ```flow ```` — aparte de
Mermaid, que sigue siendo el diagrama por defecto sin tocar).

**Herramienta de la máquina de desarrollo únicamente.** El Python
empaquetado (`runtime/`) no lleva Node, y ningún instalador ni el
`webapp/server.py` compilan nada en tiempo de ejecución. Los únicos
archivos que MARC realmente carga son los `.js`/`.css` ya compilados en
`../vendor/`, commiteados como cualquier otro asset vendorizado — igual
que `chart.min.js`/`mermaid.min.js`, que son librerías de terceros en vez
de código propio.

## Dos pipelines de build

- **`marc-interactive.ts` → `tsc` puro.** No importa nada de
  `node_modules`; usa `Chart`/`katex` como globals (`declare const ...`)
  ya vendorizados en `vendor/`. No hace falta empaquetar nada.
- **`marc-flow.tsx` → `esbuild`.** `@xyflow/react` (y React/ReactDOM) no
  ofrecen un build UMD/global como Mermaid o Chart.js — solo se
  distribuyen como paquetes de `node_modules` pensados para bundlers. Para
  vendorizarlos offline (mismo criterio que los demás: la wiki debe
  funcionar sin internet) hace falta empaquetar todo en un solo archivo,
  de ahí el bundler. El type-check corre aparte con
  `tsc -p tsconfig.flow.json --noEmit` (el `tsconfig.json` original no se
  toca, sigue apuntando solo a `marc-interactive.ts`).

## Compilar

```sh
cd frontend
npm install   # una vez, o tras cambiar de máquina
npm run build # compila ambos pipelines -> ../vendor/*.js, ../vendor/*.css
```

Después de compilar, los archivos cambiados en `vendor/` se commitean
junto con el `.ts`/`.tsx` fuente — el build real (`mkdocs build`,
disparado por `webapp/server.py`) solo copia lo que ya esté en `vendor/`,
nunca invoca `tsc` ni `esbuild`.
