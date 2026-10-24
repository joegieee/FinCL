import { COST_BEHAVIORS, INCOMING_TYPES, NEEDS, type FieldName } from './api'

// Browser-local classification presets. A preset is a named, user-created bundle
// of explicit field VALUES ("Everyday groceries" = Groceries / Variable
// routine / Essential) that fills the shared draft on demand. It never saves
// anything itself, never changes the fill-missing/replace policy, and is
// deliberately distinct from a rule: a rule matches transactions and affects
// future imports under precedence; a preset only pre-fills form fields the
// user still reviews and saves through the normal draft/preview/save path.
// Presets never store a reason, a transaction anchor, or any decision
// receipt — only field values.

export type PresetFields = Partial<Record<'category' | 'cost_behavior' | 'need' | 'incoming_type', string>>
export type Preset = { id: string; name: string; fields: PresetFields }

const STORAGE_KEY = 'fincl.classification.presets.v1'
const STORAGE_VERSION = 1
const PRESET_FIELD_NAMES: FieldName[] = ['category', 'cost_behavior', 'need', 'incoming_type']

function isPreset(value: unknown): value is Preset {
  if (!value || typeof value !== 'object') return false
  const p = value as Record<string, unknown>
  if (typeof p.id !== 'string' || !p.id) return false
  if (typeof p.name !== 'string' || !p.name.trim()) return false
  if (!p.fields || typeof p.fields !== 'object') return false
  return Object.entries(p.fields as Record<string, unknown>).every(([key, fieldValue]) =>
    (PRESET_FIELD_NAMES as string[]).includes(key) && typeof fieldValue === 'string' && fieldValue !== '')
}

/** Tolerates missing, malformed, wrong-version, or partially-corrupt data: an
 * unreadable store degrades to "no presets saved" rather than throwing, so it
 * never blocks ordinary transaction edits. */
export function loadPresets(): Preset[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || parsed.version !== STORAGE_VERSION || !Array.isArray(parsed.presets)) return []
    return parsed.presets.filter(isPreset)
  } catch { return [] }
}

/** Best-effort persistence: private browsing, disabled storage, or a full
 * quota all fail silently and leave the in-memory list the caller already
 * has, rather than surfacing a blocking error for a non-essential feature. */
function persist(presets: Preset[]): boolean {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ version: STORAGE_VERSION, presets })); return true }
  catch { return false }
}

function makeId(): string {
  try { return crypto.randomUUID() } catch { return `preset-${Date.now()}-${Math.random().toString(36).slice(2)}` }
}

export function savePreset(name: string, fields: PresetFields): Preset[] {
  const trimmed = name.trim()
  const next = [...loadPresets(), { id: makeId(), name: trimmed, fields }]
  persist(next)
  return next
}

export function renamePreset(id: string, name: string): Preset[] {
  const trimmed = name.trim()
  const next = loadPresets().map(p => p.id === id ? { ...p, name: trimmed } : p)
  persist(next)
  return next
}

export function deletePreset(id: string): Preset[] {
  const next = loadPresets().filter(p => p.id !== id)
  persist(next)
  return next
}

export type FieldApplicability = { category: boolean; cost_behavior: boolean; need: boolean; incoming_type: boolean }

const VALID_VALUES: Record<'cost_behavior' | 'need' | 'incoming_type', readonly string[]> = {
  cost_behavior: COST_BEHAVIORS, need: NEEDS, incoming_type: INCOMING_TYPES,
}

export type PresetResolution = {
  /** Only the fields that are applicable here and still reference something
   * real — safe to hand straight to the draft's setters. */
  fields: PresetFields
  /** True when the preset's category is applicable here but is no longer a
   * canonical, assignable category (deleted, archived, or renamed) — the
   * caller should say so instead of silently dropping or remapping it. */
  staleCategory: boolean
}

/** Applies applicability and current-category validation to a stored preset,
 * mirroring the same bucket-driven applicability the editors already use so
 * a preset never fills a field the server (or the visible form) would
 * reject. `assignableCategories` should already exclude archived/internal
 * categories, matching what the category picker itself offers. */
export function resolvePreset(preset: Preset, applicable: FieldApplicability, assignableCategories: string[]): PresetResolution {
  const fields: PresetFields = {}
  let staleCategory = false
  const category = preset.fields.category
  if (category !== undefined && applicable.category) {
    if (assignableCategories.includes(category)) fields.category = category
    else staleCategory = true
  }
  for (const name of ['cost_behavior', 'need', 'incoming_type'] as const) {
    const value = preset.fields[name]
    if (value !== undefined && applicable[name] && (VALID_VALUES[name] as readonly string[]).includes(value)) fields[name] = value
  }
  return { fields, staleCategory }
}
