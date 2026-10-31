import { useState } from 'react'
import { Check } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { useLedgerCommand } from '@/lib/command'

export type ClosingAccount = { id: string; displayName: string; lastActivity: string }

/**
 * Archive an account from the day its statements stop, so months after it stop
 * counting as missing and no longer hold the baseline window back. The display
 * name is sent unchanged: the details decision carries both.
 */
export function CloseAccountForm({ account, generation, onCancel, onSaved }: {
  account: ClosingAccount
  generation: string
  onCancel: () => void
  onSaved: (status: string) => void | Promise<void>
}) {
  const [closedOn, setClosedOn] = useState(account.lastActivity)
  const [reason, setReason] = useState('Account closed')
  const cmd = useLedgerCommand(generation, onSaved)
  const ready = closedOn !== '' && reason.trim() !== ''

  return (
    <Dialog open onOpenChange={open => { if (!open) onCancel() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{account.displayName} is closed</DialogTitle>
          <DialogDescription>
            Its history stays. Months after the date below stop being treated as missing statements, so the
            account no longer holds the baseline window back. You can clear the date later on the Accounts page.
          </DialogDescription>
        </DialogHeader>
        <form
          className="account-details-form"
          onChange={cmd.reset}
          onSubmit={e => {
            e.preventDefault()
            if (!ready) return
            void cmd.act({
              kind: 'ACCOUNT_DETAILS_SET', anchors: { account: account.id },
              payload: { display_name: account.displayName, archived_on: closedOn, reason: reason.trim() },
            })
          }}
        >
          <div className="form-row-3">
            <label>Closed on<Input type="date" required value={closedOn} disabled={cmd.attempted} onChange={e => setClosedOn(e.target.value)} /></label>
            <label>Reason<Input required maxLength={2000} value={reason} disabled={cmd.attempted} onChange={e => setReason(e.target.value)} /></label>
          </div>
          {cmd.error && <p className="form-error" role="alert">{cmd.error}</p>}
          {cmd.previewed && <div className="preview" role="status"><Check size={15} /><p>Mark {account.displayName} closed on {closedOn}. No transactions change.</p></div>}
          <div className="dialog-actions">
            <Button type="button" variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>
            <Button type="submit" disabled={cmd.busy || !ready}>
              {cmd.busy ? 'Working…' : cmd.previewed ? (cmd.attempted ? 'Retry same save' : 'Mark closed') : 'Preview'}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
