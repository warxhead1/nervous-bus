#!/usr/bin/env bash
# bd post-create hook — publishes bus.bead.created when a new bead is filed.
#
# Install per-project: bd hook install --event post-create --cmd "$NERVOUS_BUS/sdk/shell/hooks/bd-post-create.sh"
# Or symlink into ~/.config/bd/hooks/post-create.d/
#
# bd passes these env vars to post-create hooks:
#   BD_BEAD_ID, BD_PROJECT, BD_TITLE, BD_TYPE, BD_PRIORITY, BD_FILER,
#   BD_DESCRIPTION (first 1000 chars), BD_LABELS (comma-sep), BD_DEPENDS_ON

set -euo pipefail

NERVOUS="${NERVOUS_BUS_SDK:-$(dirname "$(readlink -f "$0")")/../nervous}"

# Resolve project name from repo root if not set
if [[ -z "${BD_PROJECT:-}" ]]; then
    BD_PROJECT="$(basename "$(git -C "${BD_REPO_ROOT:-$PWD}" rev-parse --show-toplevel 2>/dev/null || basename "$PWD")")"
fi

has_acceptance() {
    # True if the bead has machine-readable acceptance (YAML block in description)
    echo "${BD_DESCRIPTION:-}" | grep -q "^acceptance:" && echo true || echo false
}

labels_json() {
    local IFS=,
    local labels=()
    for l in ${BD_LABELS:-}; do
        [[ -n "$l" ]] && labels+=("$(printf '%s' "$l" | jq -R .)")
    done
    printf '[%s]' "$(IFS=,; echo "${labels[*]:-}")"
}

depends_json() {
    local IFS=,
    local deps=()
    for d in ${BD_DEPENDS_ON:-}; do
        [[ -n "$d" ]] && deps+=("$(printf '%s' "$d" | jq -R .)")
    done
    printf '[%s]' "$(IFS=,; echo "${deps[*]:-}")"
}

excerpt="$(printf '%s' "${BD_DESCRIPTION:-}" | head -c 1000 | jq -Rs .)"

payload="$(jq -n \
    --arg bead_id "${BD_BEAD_ID:-unknown}" \
    --arg project "$BD_PROJECT" \
    --arg title "${BD_TITLE:-}" \
    --arg type "${BD_TYPE:-task}" \
    --argjson priority "${BD_PRIORITY:-2}" \
    --arg filer "${BD_FILER:-human:unknown}" \
    --argjson excerpt "$excerpt" \
    --argjson labels "$(labels_json)" \
    --argjson depends_on "$(depends_json)" \
    --argjson has_ac "$(has_acceptance)" \
    '{
        bead_id: $bead_id,
        project: $project,
        title: $title,
        type: $type,
        priority: $priority,
        filer: $filer,
        description_excerpt: $excerpt,
        labels: $labels,
        depends_on: $depends_on,
        has_acceptance_criteria: $has_ac
    }')"

"$NERVOUS" publish bus.bead.created "$payload"
