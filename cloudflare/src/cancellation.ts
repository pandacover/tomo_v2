export type DestroyableSandbox = {
  destroy(): Promise<void>;
};

export type CancellationResult = "destroyed" | "destroy_failed" | "no_sandbox";

/**
 * Abort local stream consumption immediately, then terminate the container so
 * the guest process cannot continue spending provider tokens in the background.
 */
export async function cancelSupersededExecution(
  controller: AbortController | null,
  sandbox: DestroyableSandbox | null,
): Promise<CancellationResult> {
  controller?.abort();
  if (!sandbox) return "no_sandbox";
  try {
    await sandbox.destroy();
    return "destroyed";
  } catch {
    return "destroy_failed";
  }
}
