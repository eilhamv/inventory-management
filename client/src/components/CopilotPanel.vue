<template>
  <Teleport to="body">
    <Transition name="copilot">
      <div v-if="isOpen" class="copilot-backdrop" @click.self="$emit('close')">
        <aside class="copilot-panel" role="dialog" aria-modal="true" :aria-label="t('copilot.title')" @keydown.esc="$emit('close')">
          <header class="copilot-header">
            <div>
              <div class="copilot-title-row">
                <h2 class="copilot-title">{{ t('copilot.title') }}</h2>
                <span v-if="mode" :class="['mode-badge', mode]" :title="mode === 'demo' ? t('copilot.demoHint') : model">
                  {{ mode === 'demo' ? t('copilot.demoBadge') : t('copilot.liveBadge') }}
                </span>
              </div>
              <p class="copilot-subtitle">{{ t('copilot.subtitle') }}</p>
            </div>
            <div class="header-actions">
              <button v-if="messages.length" class="text-btn" @click="newChat">{{ t('copilot.newChat') }}</button>
              <button class="icon-btn" :aria-label="t('copilot.close')" @click="$emit('close')">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6 6 18M6 6l12 12" /></svg>
              </button>
            </div>
          </header>

          <div ref="scroller" class="copilot-body" aria-live="polite">
            <div v-if="!messages.length" class="empty">
              <p v-if="mode === 'demo'" class="demo-hint">{{ t('copilot.demoHint') }}</p>
              <button v-for="key in suggestionKeys" :key="key" class="suggestion" @click="send(t(`copilot.suggestions.${key}`))">
                {{ t(`copilot.suggestions.${key}`) }}
              </button>
            </div>

            <div v-for="msg in messages" :key="msg.id" :class="['message', msg.role]">
              <template v-if="msg.role === 'user'">
                <div class="bubble user-bubble">{{ msg.text }}</div>
              </template>
              <template v-else>
                <div v-if="msg.tools.length" class="tool-chips">
                  <span v-for="(tool, i) in msg.tools" :key="`${tool}-${i}`" :class="['tool-chip', { active: msg.streaming && i === msg.tools.length - 1 && !msg.text }]">
                    {{ t(`copilot.tools.${tool}`) }}
                  </span>
                </div>
                <div v-if="msg.text" class="bubble assistant-bubble" v-html="renderText(msg.text)"></div>
                <div v-else-if="msg.streaming && !msg.tools.length" class="bubble assistant-bubble thinking">{{ t('copilot.thinking') }}</div>
                <div v-if="msg.error" class="message-error">{{ t(`copilot.errors.${msg.error}`) }}</div>

                <div v-if="msg.draft" class="draft-card">
                  <div class="draft-title">{{ t('copilot.draft.title') }}</div>
                  <div v-for="line in msg.draft.items" :key="line.sku" class="draft-line">
                    <span>{{ translateProductName(line.name) }} <span class="sku">{{ line.sku }}</span></span>
                    <span>{{ line.quantity }} x {{ formatMoney(line.unit_price) }}</span>
                  </div>
                  <div class="draft-summary">
                    <span>{{ t('copilot.draft.total') }} <strong>{{ formatMoney(msg.draft.total_value) }}</strong></span>
                    <span>{{ t('copilot.draft.leadTime') }} <strong>{{ t('copilot.draft.days', { days: msg.draft.lead_time_days }) }}</strong></span>
                  </div>
                  <div v-if="msg.placed" class="draft-placed">
                    {{ t('copilot.draft.placed', { number: msg.placed.order_number }) }}
                    <router-link to="/orders" @click="$emit('close')">{{ t('copilot.draft.viewOrders') }}</router-link>
                  </div>
                  <template v-else>
                    <button class="confirm-btn" :disabled="msg.placing || msg.streaming" @click="confirmDraft(msg)">
                      {{ msg.placing ? t('copilot.draft.confirming') : t('copilot.draft.confirm') }}
                    </button>
                    <div v-if="msg.placeError" class="message-error">{{ t('copilot.draft.failed') }}: {{ msg.placeError }}</div>
                  </template>
                </div>
              </template>
            </div>
          </div>

          <form class="copilot-input" @submit.prevent="send(draftText)">
            <textarea
              ref="input"
              v-model="draftText"
              rows="2"
              maxlength="2000"
              :placeholder="t('copilot.placeholder')"
              @keydown.enter.exact.prevent="send(draftText)"
            ></textarea>
            <button v-if="busy" type="button" class="send-btn stop" @click="stop">{{ t('copilot.stop') }}</button>
            <button v-else type="submit" class="send-btn" :disabled="!draftText.trim()">{{ t('copilot.send') }}</button>
          </form>
          <div class="shortcut-hint">{{ t('copilot.shortcut') }}</div>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<script>
import { ref, computed, watch, nextTick } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { useFilters } from '../composables/useFilters'
import { formatCurrencyWithDecimals } from '../utils/currency'

const SUGGESTION_KEYS = ['restock', 'lowStock', 'fulfillment', 'backlog']

const escapeHtml = (text) => text
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;')

export default {
  name: 'CopilotPanel',
  props: {
    isOpen: {
      type: Boolean,
      required: true
    }
  },
  emits: ['close'],
  setup(props) {
    const { t, currentLocale, currentCurrency, translateProductName } = useI18n()
    const { selectedPeriod, selectedLocation, selectedCategory, selectedStatus } = useFilters()

    const mode = ref(null)
    const model = ref(null)
    const messages = ref([])
    const draftText = ref('')
    const conversationId = ref(null)
    const scroller = ref(null)
    const input = ref(null)
    let controller = null
    let nextId = 0

    const busy = computed(() => messages.value.some(m => m.streaming))

    const formatMoney = (amount) => formatCurrencyWithDecimals(amount, currentCurrency.value, 2)

    // Model and demo output is escaped first; only **bold** and "- " bullets become markup
    const renderText = (text) => {
      const html = escapeHtml(text).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      const out = []
      let inList = false
      for (const line of html.split('\n')) {
        const bullet = line.match(/^\s*[-*]\s+(.*)$/)
        if (bullet && !inList) { out.push('<ul>'); inList = true }
        if (!bullet && inList) { out.push('</ul>'); inList = false }
        out.push(bullet ? `<li>${bullet[1]}</li>` : (line ? `<p>${line}</p>` : ''))
      }
      if (inList) out.push('</ul>')
      return out.join('')
    }

    const scrollToBottom = () => nextTick(() => {
      if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight
    })

    const loadStatus = async () => {
      try {
        const status = await api.getCopilotStatus()
        mode.value = status.mode
        model.value = status.model
      } catch (err) {
        mode.value = null
      }
    }

    watch(() => props.isOpen, (open) => {
      if (!open) return
      if (!mode.value) loadStatus()
      nextTick(() => input.value && input.value.focus())
      scrollToBottom()
    })

    const send = async (text) => {
      const message = (text || '').trim()
      if (!message || busy.value) return
      draftText.value = ''

      messages.value.push({ id: ++nextId, role: 'user', text: message })
      messages.value.push({ id: ++nextId, role: 'assistant', text: '', tools: [], draft: null, error: null, streaming: true })
      // Mutate through the reactive proxy so the view updates as events arrive
      const reply = messages.value[messages.value.length - 1]
      scrollToBottom()

      controller = new AbortController()
      try {
        await api.streamCopilotChat({
          message,
          conversation_id: conversationId.value,
          locale: currentLocale.value,
          filters: {
            warehouse: selectedLocation.value,
            category: selectedCategory.value,
            status: selectedStatus.value,
            month: selectedPeriod.value
          }
        }, (event, data) => {
          if (event === 'meta') {
            conversationId.value = data.conversation_id
            mode.value = data.mode
          } else if (event === 'tool') {
            reply.tools.push(data.name)
          } else if (event === 'text') {
            reply.text += data.delta
          } else if (event === 'draft_order') {
            reply.draft = data
          } else if (event === 'error') {
            reply.error = data.code
          }
          scrollToBottom()
        }, controller.signal)
      } catch (err) {
        if (err.name !== 'AbortError') reply.error = 'network'
      } finally {
        reply.streaming = false
        controller = null
      }
    }

    const stop = () => controller && controller.abort()

    const newChat = () => {
      stop()
      messages.value = []
      conversationId.value = null
      nextTick(() => input.value && input.value.focus())
    }

    const confirmDraft = async (msg) => {
      try {
        msg.placing = true
        msg.placeError = null
        // Budget equals the draft total: the server re-prices and rejects anything above it
        msg.placed = await api.placeRestockingOrder({
          budget: msg.draft.total_value,
          items: msg.draft.items.map(line => ({ sku: line.sku, quantity: line.quantity }))
        })
      } catch (err) {
        msg.placeError = err.response?.data?.detail || err.message
      } finally {
        msg.placing = false
      }
    }

    return {
      t,
      mode,
      model,
      messages,
      draftText,
      scroller,
      input,
      busy,
      suggestionKeys: SUGGESTION_KEYS,
      formatMoney,
      renderText,
      translateProductName,
      send,
      stop,
      newChat,
      confirmDraft
    }
  }
}
</script>

<style scoped>
.copilot-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  z-index: 1000;
  display: flex;
  justify-content: flex-end;
}

.copilot-panel {
  width: min(440px, 100vw);
  height: 100%;
  background: #ffffff;
  display: flex;
  flex-direction: column;
  box-shadow: -8px 0 24px rgba(15, 23, 42, 0.12);
}

.copilot-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 1.25rem 1.25rem 1rem;
  border-bottom: 1px solid #e2e8f0;
}

.copilot-title-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.copilot-title {
  font-size: 1.125rem;
  font-weight: 700;
  color: #0f172a;
  margin: 0;
}

.copilot-subtitle {
  margin: 0.25rem 0 0;
  font-size: 0.813rem;
  color: #64748b;
}

.mode-badge {
  font-size: 0.688rem;
  font-weight: 600;
  padding: 0.125rem 0.5rem;
  border-radius: 999px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.mode-badge.demo {
  background: #fef3c7;
  color: #92400e;
}

.mode-badge.live {
  background: #dbeafe;
  color: #1e40af;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 0.25rem;
}

.text-btn,
.icon-btn {
  background: none;
  border: none;
  color: #64748b;
  cursor: pointer;
  font-size: 0.813rem;
  padding: 0.375rem 0.5rem;
  border-radius: 6px;
}

.text-btn:hover,
.icon-btn:hover {
  background: #f1f5f9;
  color: #0f172a;
}

.copilot-body {
  flex: 1;
  overflow-y: auto;
  padding: 1rem 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.empty {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.demo-hint {
  font-size: 0.813rem;
  color: #92400e;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 8px;
  padding: 0.625rem 0.75rem;
  margin: 0 0 0.5rem;
}

.suggestion {
  text-align: left;
  background: #f8fafc;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  padding: 0.625rem 0.75rem;
  font-size: 0.875rem;
  color: #0f172a;
  cursor: pointer;
}

.suggestion:hover {
  border-color: #94a3b8;
  background: #f1f5f9;
}

.message {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.message.user {
  align-items: flex-end;
}

.bubble {
  max-width: 92%;
  font-size: 0.875rem;
  line-height: 1.5;
  border-radius: 12px;
  padding: 0.625rem 0.875rem;
}

.user-bubble {
  background: #0f172a;
  color: #ffffff;
  white-space: pre-wrap;
}

.assistant-bubble {
  background: #f1f5f9;
  color: #0f172a;
}

.assistant-bubble :deep(p) {
  margin: 0 0 0.375rem;
}

.assistant-bubble :deep(p:last-child) {
  margin-bottom: 0;
}

.assistant-bubble :deep(ul) {
  margin: 0.25rem 0 0.375rem;
  padding-left: 1.125rem;
}

.thinking {
  color: #64748b;
  font-style: italic;
}

.tool-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.375rem;
}

.tool-chip {
  font-size: 0.75rem;
  color: #475569;
  background: #ffffff;
  border: 1px solid #e2e8f0;
  border-radius: 999px;
  padding: 0.125rem 0.625rem;
}

.tool-chip.active {
  border-color: #3b82f6;
  color: #1d4ed8;
  animation: pulse 1.2s ease-in-out infinite;
}

@keyframes pulse {
  50% { opacity: 0.55; }
}

.message-error {
  font-size: 0.813rem;
  color: #991b1b;
}

.draft-card {
  border: 1px solid #cbd5e1;
  border-radius: 10px;
  padding: 0.875rem;
  display: flex;
  flex-direction: column;
  gap: 0.375rem;
}

.draft-title {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #64748b;
}

.draft-line,
.draft-summary {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.813rem;
  color: #0f172a;
}

.draft-summary {
  border-top: 1px solid #e2e8f0;
  padding-top: 0.5rem;
  margin-top: 0.25rem;
}

.sku {
  color: #64748b;
  font-size: 0.75rem;
}

.confirm-btn {
  margin-top: 0.375rem;
  background: #0f172a;
  color: #ffffff;
  border: none;
  border-radius: 8px;
  padding: 0.5rem 0.875rem;
  font-size: 0.813rem;
  font-weight: 600;
  cursor: pointer;
}

.confirm-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.draft-placed {
  font-size: 0.813rem;
  color: #166534;
  background: #f0fdf4;
  border-radius: 6px;
  padding: 0.5rem 0.625rem;
}

.draft-placed a {
  margin-left: 0.375rem;
  font-weight: 600;
  color: inherit;
}

.copilot-input {
  display: flex;
  gap: 0.5rem;
  padding: 0.875rem 1.25rem 0.375rem;
  border-top: 1px solid #e2e8f0;
}

.copilot-input textarea {
  flex: 1;
  resize: none;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  padding: 0.5rem 0.625rem;
  font: inherit;
  font-size: 0.875rem;
}

.copilot-input textarea:focus {
  outline: none;
  border-color: #3b82f6;
}

.send-btn {
  background: #0f172a;
  color: #ffffff;
  border: none;
  border-radius: 8px;
  padding: 0 1rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
}

.send-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.send-btn.stop {
  background: #dc2626;
}

.shortcut-hint {
  font-size: 0.688rem;
  color: #94a3b8;
  text-align: right;
  padding: 0 1.25rem 0.75rem;
}

.copilot-enter-active,
.copilot-leave-active {
  transition: opacity 0.2s ease;
}

.copilot-enter-active .copilot-panel,
.copilot-leave-active .copilot-panel {
  transition: transform 0.2s ease;
}

.copilot-enter-from,
.copilot-leave-to {
  opacity: 0;
}

.copilot-enter-from .copilot-panel,
.copilot-leave-to .copilot-panel {
  transform: translateX(100%);
}
</style>
