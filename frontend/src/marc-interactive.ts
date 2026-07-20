/**
 * Logica interactiva de la wiki: graficas Chart.js y formulas KaTeX.
 * Punto de extension para Material Design Icons a futuro -- ver el final
 * de este archivo.
 *
 * Enganchada al observable global `document$` que expone mkdocs-material
 * para `extra_javascript` (documentado en su sitio, "Additional
 * JavaScript"): emite una vez por CADA navegacion dentro de la wiki, no
 * solo en la carga inicial. Hace falta porque `navigation.instant` esta
 * activo en mkdocs.yml -- Material reemplaza el <body> via fetch en vez
 * de recargar la pagina completa, asi que el contexto de JS nunca se
 * reinicia solo entre paginas. Sin este enganche, cualquier grafica
 * montada en una pagina anterior seguiria viva (y consumiendo RAM)
 * despues de navegar a otra, porque Chart.js no se destruye solo cuando
 * su <canvas> desaparece del DOM.
 *
 * Un solo archivo fuente, sin modulos ES ni bundler -- mismo criterio de
 * simplicidad que el resto del proyecto (ver docstring de
 * webapp/server.py). Compila a vendor/marc-interactive.js
 * (frontend/README.md tiene el comando); ese archivo compilado es el que
 * mkdocs.yml realmente carga via extra_javascript, igual que
 * vendor/mermaid.min.js.
 */

declare const document$: { subscribe(fn: () => void): void };

interface ChartInstance {
  destroy(): void;
}

declare const Chart: {
  new (ctx: CanvasRenderingContext2D, config: unknown): ChartInstance;
};

declare const katex: {
  render(
    tex: string,
    el: HTMLElement,
    options?: { displayMode?: boolean; throwOnError?: boolean },
  ): void;
};

// ---------------------------------------------------------------------
// Graficas -- bloque ```chart en el Markdown (ver mkdocs.yml,
// custom_fences: convierte el bloque en <pre class="chart-block">, el
// mismo mecanismo generico que ya usaba el bloque mermaid existente).
// ---------------------------------------------------------------------

const CHART_BLOCK_SELECTOR = "pre.chart-block";

// Vive a nivel de modulo (no dentro de una funcion) a proposito: debe
// sobrevivir entre la limpieza de una pagina y el montaje de la
// siguiente, cada vez que document$ emite.
const liveCharts = new Map<HTMLCanvasElement, ChartInstance>();
let chartSentinel: IntersectionObserver | null = null;

function renderChartBlock(block: HTMLElement): void {
  let config: unknown;
  try {
    config = JSON.parse(block.textContent ?? "");
  } catch {
    block.textContent = "Grafica invalida: el JSON no se pudo interpretar.";
    return;
  }
  const canvas = document.createElement("canvas");
  block.replaceWith(canvas);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  liveCharts.set(canvas, new Chart(ctx, config));
}

function mountCharts(): void {
  const blocks = document.querySelectorAll<HTMLElement>(CHART_BLOCK_SELECTOR);
  if (blocks.length === 0) return;
  // rootMargin: el centinela avisa un poco antes de que el bloque entre
  // en pantalla (no exactamente al primer pixel visible), para que la
  // grafica ya este pintada cuando el usuario termine el scroll.
  chartSentinel = new IntersectionObserver(
    (entries, observer) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        renderChartBlock(entry.target as HTMLElement);
        observer.unobserve(entry.target);
      }
    },
    { rootMargin: "200px" },
  );
  blocks.forEach((block) => chartSentinel!.observe(block));
}

function unmountCharts(): void {
  chartSentinel?.disconnect();
  chartSentinel = null;
  liveCharts.forEach((chart) => chart.destroy());
  liveCharts.clear();
}

// ---------------------------------------------------------------------
// Formulas -- burbujas de pymdownx.arithmatex (ver mkdocs.yml,
// generic: true + wraps en blanco: la burbuja lleva el LaTeX puro, sin
// delimitadores \(...\)/\[...\] que volver a parsear aqui). Sin registro
// ni destroy(): katex.render() solo escribe HTML/MathML estatico dentro
// del elemento, no deja listeners ni objetos vivos que limpiar -- cuando
// Material reemplaza el <body> en la siguiente navegacion, se va con el
// resto. Tampoco se hace perezoso con IntersectionObserver como las
// graficas: renderizar una formula es ordenes de magnitud mas barato que
// montar un <canvas> con Chart.js, el centinela costaria mas de lo que
// ahorra.
// ---------------------------------------------------------------------

const MATH_SELECTOR = ".arithmatex";

function mountMath(): void {
  document.querySelectorAll<HTMLElement>(MATH_SELECTOR).forEach((el) => {
    katex.render(el.textContent ?? "", el, {
      displayMode: el.tagName === "DIV",
      throwOnError: false,
    });
  });
}

// ---------------------------------------------------------------------
// Ciclo de vida por pagina. El orden importa: limpiar SIEMPRE antes de
// montar -- document$ emite despues de que Material ya reemplazo el
// <body>, asi que en este punto los bloques de la pagina anterior ya no
// estan en el DOM, pero sus instancias de Chart.js si siguen vivas en
// `liveCharts` hasta que unmountCharts() las destruye explicitamente.
//
// Punto de extension para Material Design Icons (a futuro): si necesita
// logica propia de montaje por pagina, agregar su propia funcion y
// llamarla aqui -- no hace falta una segunda suscripcion a document$.
// ---------------------------------------------------------------------

document$.subscribe(() => {
  unmountCharts();
  mountCharts();
  mountMath();
});
