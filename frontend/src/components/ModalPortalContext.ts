import { createContext, useContext } from 'react'

export const ModalPortalContext = createContext<{ current: HTMLDivElement | null } | null>(null)

/** 让子组件把浮层 Portal 挂到当前 Modal 的焦点作用域内。 */
export function useModalPortalContainer() {
  return useContext(ModalPortalContext)
}
