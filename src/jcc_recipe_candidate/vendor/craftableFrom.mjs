/** Completed items you can build right now from the components you hold (respects multiplicity). */
export function craftableFrom(heldComponents, recipes) {
  const have = {};
  for (const c of heldComponents || []) have[c] = (have[c] || 0) + 1;
  const out = [];
  for (const [item, r] of Object.entries(recipes)) {
    const need = {};
    for (const c of r.components) need[c] = (need[c] || 0) + 1;
    if (Object.entries(need).every(([c, n]) => (have[c] || 0) >= n)) {
      out.push({ item, name: r.name, components: r.components });
    }
  }
  return out;
}
