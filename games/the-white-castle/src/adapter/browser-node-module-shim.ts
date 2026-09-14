/**
 * Browser build boundary for host-only route enumeration modules.
 *
 * Enumeration runs in BGLab's owned Node worker. The browser Adapter remains the
 * authority for restore, view, dispatch, snapshot, and finalResult only.
 */
export function createRequire(_url: string): (specifier: string) => Record<string, unknown> {
  return (specifier: string) => {
    const headlessOnly = (): never => {
      throw new Error(
        "Route enumeration is available only in the headless Adapter process.",
      );
    };
    if (specifier === "../../../_sdk/decision-explorer/index.cjs") {
      return {
        createDecisionExplorer:headlessOnly,
        createDecisionMap:headlessOnly,
        createAuthorityGateway() {
          return {
            outcomeIndex:headlessOnly,
            enumerateRoutes:headlessOnly,
            // Browser lifecycle calls invalidate during start/restore/dispatch.
            // It is safe because this boundary never creates a headless cache.
            invalidate:() => ({ released:false }),
          };
        },
      };
    }
    if (specifier === "../../../_sdk/semantic-route-automaton/index.cjs") {
      return { createSemanticRouteAutomaton:headlessOnly };
    }
    throw new Error(`Unsupported browser module: ${specifier}`);
  };
}
