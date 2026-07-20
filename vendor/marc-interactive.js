"use strict";
const CHART_BLOCK_SELECTOR = "pre.chart-block";
const liveCharts = new Map();
let chartSentinel = null;
function renderChartBlock(block) {
    var _a;
    let config;
    try {
        config = JSON.parse((_a = block.textContent) !== null && _a !== void 0 ? _a : "");
    }
    catch {
        block.textContent = "Grafica invalida: el JSON no se pudo interpretar.";
        return;
    }
    const canvas = document.createElement("canvas");
    block.replaceWith(canvas);
    const ctx = canvas.getContext("2d");
    if (!ctx)
        return;
    liveCharts.set(canvas, new Chart(ctx, config));
}
function mountCharts() {
    const blocks = document.querySelectorAll(CHART_BLOCK_SELECTOR);
    if (blocks.length === 0)
        return;
    chartSentinel = new IntersectionObserver((entries, observer) => {
        for (const entry of entries) {
            if (!entry.isIntersecting)
                continue;
            renderChartBlock(entry.target);
            observer.unobserve(entry.target);
        }
    }, { rootMargin: "200px" });
    blocks.forEach((block) => chartSentinel.observe(block));
}
function unmountCharts() {
    chartSentinel === null || chartSentinel === void 0 ? void 0 : chartSentinel.disconnect();
    chartSentinel = null;
    liveCharts.forEach((chart) => chart.destroy());
    liveCharts.clear();
}
const MATH_SELECTOR = ".arithmatex";
function mountMath() {
    document.querySelectorAll(MATH_SELECTOR).forEach((el) => {
        var _a;
        katex.render((_a = el.textContent) !== null && _a !== void 0 ? _a : "", el, {
            displayMode: el.tagName === "DIV",
            throwOnError: false,
        });
    });
}
document$.subscribe(() => {
    unmountCharts();
    mountCharts();
    mountMath();
});
