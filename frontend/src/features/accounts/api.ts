import { api } from '@/lib/api'

export type PocketCoverage = {
  pocket: string
  expected_start: string | null
  expected_end: string | null
  observed_start: string | null
  observed_end: string | null
  entry_count: number
  coverage_status: 'covered' | 'gap' | 'unconfigured'
  /** Days between this account's statements that no statement covers. */
  gaps: { start: string; end: string }[]
}

export type AccountRow = {
  id: string
  institution: string
  source_type: string
  display_name: string
  masked_identifier: string | null
  archived_on: string
  entry_count: number
  currencies: string[]
  pockets: PocketCoverage[]
  coverage_status: 'covered' | 'gap' | 'unconfigured'
  expected_start: string | null
  expected_end: string | null
}

export type AliasRow = { source_identity: string; account_id: string; reason: string }

export type AccountsPage = {
  generation: string
  accounts: AccountRow[]
  aliases: AliasRow[]
  global_coverage: { covered?: boolean; reasons?: string[]; [k: string]: unknown }
}

export const loadAccounts = () => api<AccountsPage>('accounts')
