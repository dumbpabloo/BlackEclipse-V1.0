import { dirname } from "path";
import { fileURLToPath } from "url";
import { FlatCompat } from "@eslint/eslintrc";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const compat = new FlatCompat({
  baseDirectory: __dirname,
});

const eslintConfig = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // Allow intentional interface-stub params (e.g. provider methods that
      // are no-ops until a later phase) to be prefixed with underscore.
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    ignores: [
      "node_modules/**",
      ".next/**",
      // The dev build writes to `.next-dev` (separate from `.next`); it is
      // machine-generated output and must be ignored or lint reports hundreds of
      // errors in vendored Next.js runtime files.
      ".next-dev/**",
      // Live mode (`npm run desktop:live`) writes .next-live-<channel>/; also
      // machine-generated, so lint never walks it.
      ".next-live-*/**",
      // The desktop launchers (`npm run desktop:*`) build into .next-desktop /
      // .next-desktop-dev, and a promotion copies whole app trees into
      // releases/. All three are gitignored build output: linting them reports
      // ~150k errors inside vendored Next.js/plotly files and fails the gate for
      // anyone who has simply run the app locally.
      ".next-desktop/**",
      ".next-desktop-dev/**",
      "releases/**",
      // The installer payload assembled by `npm run desktop:package` (it vendors
      // the Python packs, so it contains thousands of plotly/chart JS files).
      "dist/**",
      "out/**",
      "build/**",
      "next-env.d.ts",
      // Generated Prisma client — vendored output, never hand-edited.
      "src/generated/**",
      // One-off Node import/utility scripts (plain JS, not part of the app).
      "scripts/**",
      "bin/**",
      // Disposable root-level Node scratch scripts (Playwright probes, ad-hoc
      // verification). The `_` prefix marks them throwaway, mirroring the
      // unused-var convention; they are never app source and must not gate lint.
      "_*.cjs",
      "_*.mjs",
      // Disposable agent artifacts: isolated code workspaces, the code index and
      // workspace index. These are machine-generated, may be mid-delete, and are
      // never source. Linting them crashes on a partially-removed worktree.
      ".black-eclipse/**",
      ".tmp-tests/**",
      // Runtime state: the staged update tree, the parked `node_modules`
      // (`.runtime/deps-previous`) and the update job/deps scratch dirs. A single
      // in-app update stages a whole dependency tree here (~22k JS files on this
      // machine), so `npx eslint .` walked it and took over an hour. It is
      // machine-generated, may be mid-delete, and is never source.
      ".runtime/**",
    ],
  },
];

export default eslintConfig;
