import babel from '@rolldown/plugin-babel';
import tailwindcss from '@tailwindcss/vite';
import react, { reactCompilerPreset } from '@vitejs/plugin-react';
import { defineConfig } from 'waku/config';

export default defineConfig({
  vite: {
    plugins: [
      tailwindcss(),
      react(),
      babel({ presets: [reactCompilerPreset()] }),
    ],
    ssr: {
      external: ['better-sqlite3', 'bindings'],
    },
    build: {
      rollupOptions: {
        external: ['better-sqlite3', 'bindings'],
      },
    },
  },
});
