/**
 * Client-side path-prefix helpers for file-drop detection.
 *
 * `startsLikeDroppedPathCandidate` mirrors the broad prefix prefilter in
 * cli.py::_detect_file_drop(). It intentionally catches more than the composer
 * normally probes so busy-queue coalescing can preserve media/file boundaries and
 * let the normal send path run backend validation.
 *
 * `looksLikeDroppedPath` is the conservative composer prefilter used before an
 * immediate input.detect_drop RPC; it avoids short slash commands such as
 * `/help` and `/model sonnet`.
 */
export function startsLikeDroppedPathCandidate(text: string): boolean {
  const trimmed = text.trim()

  if (!trimmed || trimmed.includes('\n')) {
    return false
  }

  return (
    trimmed.startsWith('/') ||
    trimmed.startsWith('~') ||
    trimmed.startsWith('./') ||
    trimmed.startsWith('../') ||
    trimmed.startsWith('file://') ||
    /^[A-Za-z]:[/\\]/.test(trimmed) ||
    trimmed.startsWith('"/') ||
    trimmed.startsWith('"~') ||
    trimmed.startsWith("'/") ||
    trimmed.startsWith("'~") ||
    trimmed.startsWith('"./') ||
    trimmed.startsWith('"../') ||
    trimmed.startsWith("'./") ||
    trimmed.startsWith("'../") ||
    /^["'][A-Za-z]:[/\\]/.test(trimmed)
  )
}

export function looksLikeDroppedPath(text: string): boolean {
  const trimmed = text.trim()

  if (!startsLikeDroppedPathCandidate(trimmed)) {
    return false
  }

  // Composer fast-path: avoid unnecessary RPCs for common short slash commands.
  // Slash commands are handled before this helper on the normal submission path.
  if (trimmed.startsWith('/')) {
    const rest = trimmed.slice(1)

    return rest.includes('/') || rest.includes('.')
  }

  return true
}
