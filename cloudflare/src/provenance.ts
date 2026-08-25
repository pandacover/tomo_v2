export const IMAGE_REVISION_PATH = "/opt/tomo/IMAGE_REVISION";

type RevisionResult = { exitCode: number; stdout: string };

export function observedImageRevision(result: RevisionResult): string | null {
  if (result.exitCode !== 0) return null;
  const revision = result.stdout.trim();
  return /^(?:[0-9a-f]{40}|development)$/.test(revision) ? revision : null;
}

export function imageRevisionMatches(result: RevisionResult, expected: string): boolean {
  return observedImageRevision(result) === expected;
}
