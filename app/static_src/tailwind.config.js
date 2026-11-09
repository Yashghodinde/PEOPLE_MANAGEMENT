// Design tokens live in tokens.json (single source, also read by tests/design/test_contrast.py).
// Spacing, radius, colours and font sizes REPLACE Tailwind's defaults (no `extend`), per M01.
const t = require("./tokens.json");

module.exports = {
  content: ["../templates/**/*.html", "../ots/**/templates/**/*.html", "../ots/**/*.py", "../static/js/**/*.js"],
  theme: {
    colors: { transparent: "transparent", current: "currentColor", ...t.colors },
    spacing: t.spacing,
    borderRadius: t.radius, // no `none`: rounded-none is deliberately unavailable
    fontSize: t.fontSize,
    boxShadow: { card: t.shadow.card, none: "none" },
    transitionDuration: { DEFAULT: t.motion.fast, fast: t.motion.fast, panel: t.motion.panel },
    transitionTimingFunction: { DEFAULT: "ease-out", out: "ease-out" },
    fontFamily: { sans: ["Inter", "system-ui", "sans-serif"] },
    extend: {
      width: { sidebar: t.layout.sidebar, "sidebar-collapsed": t.layout.sidebarCollapsed },
      maxWidth: { page: t.layout.maxWidth },
      gridTemplateColumns: { split: "38.2fr 61.8fr" },
    },
  },
  plugins: [],
};
