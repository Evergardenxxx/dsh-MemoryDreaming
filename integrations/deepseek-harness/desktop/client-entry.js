// Source template. Build with scripts/build_native_client.py; do not load this
// template directly. React and slots come from Harness's shared client runtime.
window.__ModuleLoader__.load({
  id: '@local/memorydreaming',
  factory(require) {
    const React = require('react')
    const css = __MEMORYDREAMING_CSS__
    /* MEMORYDREAMING_VIEW */

    function apply(ctx, config = {}) {
      if (config.enabled === false) return
      ctx.effect(() => {
        const style = document.createElement('style')
        style.dataset.memorydreaming = 'desktop'
        style.textContent = css
        document.head.append(style)
        return () => style.remove()
      }, 'memorydreaming desktop styles')

      function MemoryPage() {
        const root = React.useRef(null)
        React.useEffect(() => {
          const controller = new AbortController()
          const request = async (action, args) => {
            const response = await ctx.connection.rpc.call('/api', 'memorydreamingDesktop/request',
              { args: { action, args } }, controller.signal)
            if (!response.ok) {
              const failure = response.error ?? {}
              throw new Error(`${failure.code ?? 'UNAVAILABLE'}: ${failure.message ?? '记忆管理连接未完成'}`)
            }
            return response.value
          }
          const unmount = mountDesktop(root.current, request)
          return () => { controller.abort(); unmount() }
        }, [])
        return React.createElement('section', { className: 'memorydreaming-host' },
          React.createElement('div', { className: 'memorydreaming-host-toolbar' },
            React.createElement('button', { type: 'button', onClick: () => ctx.layout.selectPanel(null) }, '返回对话')),
          React.createElement('div', { ref: root, 'data-testid': 'memorydreaming-root' }))
      }

      ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'memorydreaming' }, MemoryPage))
      ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
        name: 'sidebar.panellist', id: 'memorydreaming', label: '记忆管理', order: 55,
      }, () => React.createElement('span', { 'aria-hidden': true }, '忆')))
    }

    return { inject: ['slots', 'layout', 'connection'], apply }
  },
})
