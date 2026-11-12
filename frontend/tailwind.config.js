/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        side: {
          bg:    "#0f1117",
          fg:    "#e1e7ef",
          muted: "#9ca3af",
          border: "#1f2937",
        },
      },
    },
  },
  plugins: [],
};