/**
 * 交易费率面板 — 实盘记账的费用/税费估算口径。
 *
 * 录单留空费用/税费时按此配置自动估算，最终以交割单导入校准为准。
 */
import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { CircleDollarSign, Loader2 } from 'lucide-react'
import { usePreferences } from '@/lib/useSharedQueries'
import { api, type TradeFeeProfile } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { PageHeader } from '@/components/PageHeader'
import { toast } from '@/components/Toast'

type FeeDraft = {
  commissionRate: string
  minCommission: string
  stampTaxRate: string
  transferFeeRate: string
}

function toDraft(profile: TradeFeeProfile): FeeDraft {
  return {
    commissionRate: String(profile.commission_rate),
    minCommission: String(profile.min_commission),
    stampTaxRate: String(profile.stamp_tax_rate),
    transferFeeRate: String(profile.transfer_fee_rate),
  }
}

export function SettingsTradeFeePanel() {
  const qc = useQueryClient()
  const { data: prefs, isLoading } = usePreferences()
  const [draft, setDraft] = useState<FeeDraft | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (draft === null && prefs?.trade_fee_profile) {
      setDraft(toDraft(prefs.trade_fee_profile))
    }
  }, [draft, prefs])

  async function save() {
    if (!draft) return
    const values: Partial<TradeFeeProfile> = {}
    const fields: [keyof TradeFeeProfile, string][] = [
      ['commission_rate', draft.commissionRate],
      ['min_commission', draft.minCommission],
      ['stamp_tax_rate', draft.stampTaxRate],
      ['transfer_fee_rate', draft.transferFeeRate],
    ]
    for (const [key, text] of fields) {
      const value = Number(text)
      if (text.trim() === '' || !Number.isFinite(value) || value < 0) {
        toast('费率需为不小于 0 的数字', 'error')
        return
      }
      values[key] = value
    }
    setSaving(true)
    try {
      const result = await api.updateTradeFeeProfile(values)
      setDraft(toDraft(result.trade_fee_profile))
      await qc.invalidateQueries({ queryKey: QK.preferences })
      toast('交易费率已保存', 'success')
    } finally {
      setSaving(false)
    }
  }

  return (
    <>
      <PageHeader
        title="交易费率"
        subtitle="录单时费用/税费留空将按此口径自动估算；与实际交割单有出入时可再导入交割单校准。"
      />

      <section className="rounded-card border border-border bg-surface p-5">
        <div className="flex items-center gap-2 mb-4">
          <CircleDollarSign className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">费率配置</h3>
        </div>

        {isLoading || draft === null ? (
          <div className="py-6"><Loader2 className="h-4 w-4 animate-spin text-muted" /></div>
        ) : (
          <>
            <FeeRow
              label="佣金费率"
              desc="双边收取，默认 0.00025（万 2.5）"
              value={draft.commissionRate}
              step={0.00001}
              onChange={v => setDraft({ ...draft, commissionRate: v })}
              disabled={saving}
            />
            <FeeRow
              label="最低佣金（元）"
              desc="单笔最低佣金，默认 5；填 0 表示免五"
              value={draft.minCommission}
              step={0.01}
              onChange={v => setDraft({ ...draft, minCommission: v })}
              disabled={saving}
            />
            <FeeRow
              label="印花税率"
              desc="仅卖出股票收取，默认 0.0005（0.05%）"
              value={draft.stampTaxRate}
              step={0.0001}
              onChange={v => setDraft({ ...draft, stampTaxRate: v })}
              disabled={saving}
            />
            <FeeRow
              label="过户费率"
              desc="仅沪市股票双边收取，默认 0.00001（0.001%）"
              value={draft.transferFeeRate}
              step={0.00001}
              onChange={v => setDraft({ ...draft, transferFeeRate: v })}
              disabled={saving}
            />

            <div className="mt-4 flex justify-end">
              <button
                onClick={save}
                disabled={saving}
                className="inline-flex h-8 items-center gap-1.5 rounded-btn bg-accent px-4 text-xs font-medium text-white disabled:opacity-50"
              >
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                保存
              </button>
            </div>
          </>
        )}
      </section>
    </>
  )
}

function FeeRow({ label, desc, value, step, onChange, disabled }: {
  label: string
  desc: string
  value: string
  step: number
  onChange: (v: string) => void
  disabled?: boolean
}) {
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="min-w-0">
        <div className="text-sm text-foreground">{label}</div>
        <div className="text-[11px] text-muted truncate">{desc}</div>
      </div>
      <input
        type="number"
        min={0}
        step={step}
        value={value}
        disabled={disabled}
        onChange={e => onChange(e.target.value)}
        className="w-32 h-8 px-2 rounded-btn border border-border bg-base font-mono text-xs text-foreground outline-none focus:border-accent/60 disabled:opacity-50 shrink-0"
      />
    </div>
  )
}
