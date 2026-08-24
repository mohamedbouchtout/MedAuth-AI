/**
 * Placeholder on purpose.
 *
 * TASK-070 builds the session management screen — patient search, start visit,
 * live transcript, nudge overlay. Wiring a half-built version of it here would
 * be the exact failure that task exists to prevent: a screen that looks like it
 * is recording when it is not. Until then this renders nothing that suggests a
 * session can be started.
 */
export function App() {
  return (
    <main className="flex min-h-screen items-center justify-center p-8">
      <p className="text-sm text-slate-500">
        MedAuth AI — provider dashboard. Session UI arrives in TASK-070.
      </p>
    </main>
  );
}
