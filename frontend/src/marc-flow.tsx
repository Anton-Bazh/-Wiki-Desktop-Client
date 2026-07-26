/**
 * Diagramas arrastrables -- bloque ```flow en el Markdown. Mermaid (bloque
 * ```mermaid) sigue siendo el diagrama por defecto, sin tocar; este es un
 * bloque aparte solo para los casos puntuales donde de verdad hace falta
 * mover nodos con el mouse, cosa que Mermaid no soporta.
 *
 * A diferencia de marc-interactive.ts (Chart.js/KaTeX, vendorizados como
 * scripts globales UMD), @xyflow/react no ofrece un build UMD -- es un
 * paquete de node_modules pensado para bundlers. Por eso este archivo se
 * compila con esbuild (ver frontend/package.json, build:flow) en vez de
 * tsc puro, y por eso vive separado de marc-interactive.ts: son dos
 * pipelines distintos, no hace falta que el archivo sin bundler se entere
 * del que si lo necesita.
 *
 * Mismo enganche a document$ que marc-interactive.ts (navigation.instant
 * activo, el <body> se reemplaza via fetch sin recargar) -- ver ese
 * archivo para el porque completo. document$ soporta multiples
 * suscriptores, asi que esta suscripcion es independiente de la de
 * marc-interactive.ts.
 */

import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { createRoot, type Root } from "react-dom/client";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Panel,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

// Mismos paths SVG que ya usa la app (webapp/templates/icons.html) para que
// el boton de cerrar del modal se vea identico al resto -- no se puede
// reusar el macro Jinja directamente (esto es contenido React), pero si el
// path. El icono de expandir es nuevo (no existia ninguno en el proyecto),
// dibujado en el mismo estilo (20x20, stroke, esquinas en L).
function ExpandIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round">
      <path d="M3 7V3h4M13 3h4v4M17 13v4h-4M7 17H3v-4" />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round">
      <path d="M5 5l10 10M15 5 5 15" />
    </svg>
  );
}

declare const document$: { subscribe(fn: () => void): void };

interface FlowSpec {
  nodes: Node[];
  edges: Edge[];
}

const FLOW_BLOCK_SELECTOR = "pre.flow-block";

// Vive a nivel de modulo, igual que liveCharts en marc-interactive.ts: debe
// sobrevivir entre la limpieza de una pagina y el montaje de la siguiente.
const liveRoots = new Map<HTMLElement, Root>();
let flowSentinel: IntersectionObserver | null = null;

// Se lee una sola vez al montar, no de forma reactiva. Limitacion conocida
// y aceptada a proposito: si el usuario cambia el toggle claro/oscuro con
// un diagrama ya en pantalla, ese diagrama no cambia de color hasta la
// proxima navegacion (mismo criterio que el limite del CDN de emoji ya
// documentado en mkdocs.yml -- se anota en vez de resolver algo que nadie
// ha pedido todavia).
function colorModeFromPage(): "light" | "dark" {
  return document.body.dataset.mdColorScheme === "slate" ? "dark" : "light";
}

function FlowDiagram({ nodes: initialNodes, edges: initialEdges }: FlowSpec) {
  // React Flow es un componente controlado: sin onNodesChange/onEdgesChange
  // (via applyNodeChanges/applyEdgeChanges, aqui detras de useNodesState/
  // useEdgesState) el arrastre no persiste -- calcula el cambio de
  // posicion pero no tiene donde escribirlo, y el nodo vuelve a la
  // posicion del prop original al soltar. Es el patron oficial de la
  // libreria para el caso mas simple (diagrama fijo que el usuario mueve
  // en pantalla, sin guardar el resultado).
  //
  // Este estado se comparte entre la vista inline y el modal grande (dos
  // <ReactFlow> distintos mas abajo, mismos nodes/edges/handlers): arrastrar
  // en uno se refleja en el otro, mismo diagrama en dos ventanas.
  const [nodes, , onNodesChange] = useNodesState(initialNodes);
  const [edges, , onEdgesChange] = useEdgesState(initialEdges);
  const [isExpanded, setIsExpanded] = useState(false);
  // Cada <ReactFlow> necesita un id distinto (via id -> rfId interno) para
  // no chocar en los <defs> SVG de las flechas cuando el inline y el modal
  // coexisten en el DOM -- y para no chocar tampoco con OTRO bloque
  // ```flow en la misma pagina, de ahi useId() en vez de un string fijo.
  const instanceId = useId();

  useEffect(() => {
    if (!isExpanded) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setIsExpanded(false);
    };
    window.addEventListener("keydown", onKeyDown);
    // Bloquea el scroll del body mientras el modal esta abierto -- a
    // diferencia del modal "Acerca de" (contenido estatico chico), aqui el
    // usuario hace scroll/zoom con la rueda DENTRO del canvas, y sin esto
    // ese scroll se filtraria a la pagina de fondo detras del overlay.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
    };
  }, [isExpanded]);

  // minZoom bajo (default es 0.5): un diagrama grande (30+ nodos) puede
  // necesitar mas zoom-out del que el minZoom por defecto permite para
  // caber completo en el contenedor via fitView -- sin esto, el diagrama
  // se ve recortado arriba/abajo aunque fitView si intento encajarlo.
  const shared = {
    nodes,
    edges,
    onNodesChange,
    onEdgesChange,
    colorMode: colorModeFromPage(),
    fitView: true,
    minZoom: 0.1,
  } as const;

  return (
    <>
      <ReactFlow id={`${instanceId}-inline`} {...shared}>
        <Background />
        <Controls />
        <MiniMap pannable zoomable />
        <Panel position="top-right">
          <button
            type="button"
            className="marc-flow-expand-btn"
            onClick={() => setIsExpanded(true)}
            aria-label="Ver diagrama en grande"
            title="Ver diagrama en grande"
          >
            <ExpandIcon />
          </button>
        </Panel>
      </ReactFlow>
      {isExpanded &&
        createPortal(
          <div
            className="marc-flow-modal-overlay"
            onClick={(e) => {
              if (e.target === e.currentTarget) setIsExpanded(false);
            }}
          >
            <div className="marc-flow-modal-panel">
              <button
                type="button"
                className="marc-flow-modal-close"
                onClick={() => setIsExpanded(false)}
                aria-label="Cerrar"
                title="Cerrar"
              >
                <CloseIcon />
              </button>
              <ReactFlow id={`${instanceId}-modal`} {...shared}>
                <Background />
                <Controls />
                <MiniMap pannable zoomable />
              </ReactFlow>
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}

function renderFlowBlock(block: HTMLElement): void {
  let spec: FlowSpec;
  try {
    spec = JSON.parse(block.textContent ?? "");
  } catch {
    block.textContent = "Diagrama invalido: el JSON no se pudo interpretar.";
    return;
  }
  const container = document.createElement("div");
  container.className = "marc-flow-container";
  block.replaceWith(container);
  const root = createRoot(container);
  root.render(<FlowDiagram nodes={spec.nodes} edges={spec.edges} />);
  liveRoots.set(container, root);
}

function mountFlows(): void {
  const blocks = document.querySelectorAll<HTMLElement>(FLOW_BLOCK_SELECTOR);
  if (blocks.length === 0) return;
  // rootMargin: mismo valor y mismo motivo que chartSentinel en
  // marc-interactive.ts -- el diagrama ya esta montado cuando el usuario
  // termina el scroll hasta el.
  flowSentinel = new IntersectionObserver(
    (entries, observer) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        renderFlowBlock(entry.target as HTMLElement);
        observer.unobserve(entry.target);
      }
    },
    { rootMargin: "200px" },
  );
  blocks.forEach((block) => flowSentinel!.observe(block));
}

function unmountFlows(): void {
  flowSentinel?.disconnect();
  flowSentinel = null;
  liveRoots.forEach((root) => root.unmount());
  liveRoots.clear();
}

document$.subscribe(() => {
  unmountFlows();
  mountFlows();
});
