import { motion, useReducedMotion } from 'framer-motion'

import { Modal } from '@/components/Modal'
import { RuleEditor } from '@/components/monitor/RuleEditor'
import type { MonitorRule, PortfolioWatchItem } from '@/lib/api'

export type WatchPoolMonitorDialogState = {
  readonly target: PortfolioWatchItem
  readonly rule: MonitorRule | null
}

type Props = {
  readonly state: WatchPoolMonitorDialogState | null
  readonly onClose: () => void
}

export function WatchPoolMonitorDialog({ state, onClose }: Props) {
  const reduceMotion = useReducedMotion()

  if (!state) return null

  return (
    <Modal
      onClose={onClose}
      ariaLabel={state.rule ? `编辑 ${state.target.name} 的监控规则` : `为 ${state.target.name} 添加监控`}
      overlayClassName="fixed inset-0 z-50 flex items-start justify-center overflow-auto bg-black/45 p-4 backdrop-blur-sm"
      panelClassName={`mt-8 w-full border-0 bg-transparent shadow-none ${state.rule ? 'max-w-3xl' : 'max-w-2xl'}`}
    >
      <motion.div
        initial={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.97, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: reduceMotion ? 0 : 0.15 }}
      >
        <RuleEditor
          rule={state.rule}
          simple={state.rule === null}
          defaultThresholdCondition={{ field: 'last_price', op: '<=' }}
          preset={{
            scope: 'symbols',
            symbols: [state.target.symbol],
            asset_type: state.target.asset_type,
            type: 'signal',
            logic: 'or',
            cooldown_seconds: 1200,
          }}
          onClose={onClose}
        />
      </motion.div>
    </Modal>
  )
}
