// Import de CSS como side-effect (ver marc-flow.tsx) -- esbuild lo empaqueta
// bien, pero tsc necesita saber que el modulo existe para el type-check.
declare module "*.css";
