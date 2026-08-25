export const CONTAINER_RETRY_ATTEMPTS = 3;
export const CONTAINER_RETRY_BASE_MS = 2_000;

type TransientClassifier = (error: unknown) => boolean;

export function retryableSandbox(error: unknown, isTransient: TransientClassifier): boolean {
  return isTransient(error);
}

export function sandboxFailureCode(error: unknown, isTransient: TransientClassifier): string {
  if (retryableSandbox(error, isTransient)) return "sandbox_transient";
  const name = error instanceof Error ? error.name : "";
  if (name === "ContainerUnavailableError") return "sandbox_unavailable";
  if (name === "RPCTransportError") return "sandbox_transport";
  const message = error instanceof Error ? error.message : "";
  if (message === "sandbox_revision_mismatch") return message;
  if (message === "missing_openrouter_key") return message;
  return "sandbox_failure";
}
