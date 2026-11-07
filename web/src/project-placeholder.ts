// SPDX-License-Identifier: AGPL-3.0-only
/** The project selector's placeholder: loading, failed and empty are different states, and empty says what to do. */
export function projectPlaceholder(state: { loaded: boolean; failed: boolean; count: number }): string | null {
  if (state.count > 0) return null;
  if (!state.loaded) return "Loading projects…";
  if (state.failed) return "Projects could not be loaded (see the error above)";
  return "No projects yet: run pados project create --name <name> --root-path <folder>";
}
