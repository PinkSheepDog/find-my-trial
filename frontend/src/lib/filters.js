// Search filters.
//
// These defaults are stated explicitly on the client and are kept identical to
// the server defaults in backend/app/api/schemas.py (MatchRequest). Previously
// the client omitted `treatment_only` and sent `interventional_only: false`
// against a server default of `true`, so the effective behaviour depended on
// which side happened to set the field. Every filter is now sent on every
// request: what the checkbox says is what the server receives.

export const DEFAULT_FILTERS = {
  top_k: 10,
  active_only: true,          // matches MatchRequest.active_only = True
  recruiting_only: false,     // newer server-side filter; sent only when supported
  interventional_only: true,  // matches MatchRequest.interventional_only = True
  treatment_only: true,       // matches MatchRequest.treatment_only = True
  location: "",
  location_required: false,   // matches MatchRequest.location_required = False
};

// Registry statuses each status filter admits. Stated in the UI so "active only"
// is not left to the user's imagination — it is NOT the same as "recruiting".
export const ACTIVE_STATUSES = [
  "RECRUITING",
  "ENROLLING_BY_INVITATION",
  "NOT_YET_RECRUITING",
  "ACTIVE_NOT_RECRUITING",
  "AVAILABLE",
];

export const RECRUITING_STATUSES = [
  "RECRUITING",
  "ENROLLING_BY_INVITATION",
  "NOT_YET_RECRUITING",
  "AVAILABLE",
];

/**
 * Drop filter keys the server does not model, so a control that cannot take
 * effect is never silently sent. `supported` of null means "capabilities
 * unknown" — send everything and let the server ignore what it does not know.
 */
export function payloadForServer(filters, supported) {
  if (!supported) return { ...filters };
  const out = {};
  Object.entries(filters).forEach(([k, v]) => {
    if (supported.has(k)) out[k] = v;
  });
  return out;
}
