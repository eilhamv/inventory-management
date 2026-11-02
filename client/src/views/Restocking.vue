<template>
  <div class="restocking">
    <div class="page-header">
      <h2>{{ t('restocking.title') }}</h2>
      <p>{{ t('restocking.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else>
      <div class="card budget-card">
        <div class="budget-header">
          <label for="budget-slider" class="budget-label">{{ t('restocking.budget') }}</label>
          <span class="budget-value">{{ formatMoney(budget) }}</span>
        </div>
        <input
          id="budget-slider"
          v-model.number="budget"
          type="range"
          class="budget-slider"
          min="0"
          :max="sliderMax"
          :step="sliderStep"
          :aria-valuetext="formatMoney(budget)"
        />
        <div class="budget-scale">
          <span>{{ formatMoney(0) }}</span>
          <span>{{ t('restocking.budgetHint', { amount: formatMoney(fullRestockCost) }) }}</span>
          <span>{{ formatMoney(sliderMax) }}</span>
        </div>
      </div>

      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t('restocking.orderTotal') }}</div>
          <div class="stat-value">{{ formatMoney(plan.total_cost) }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('restocking.remaining') }}</div>
          <div class="stat-value">{{ formatMoney(plan.remaining_budget) }}</div>
        </div>
        <div class="stat-card warning">
          <div class="stat-label">{{ t('restocking.leadTime') }}</div>
          <div class="stat-value">
            {{ plan.items.length ? t('restocking.leadTimeDays', { days: orderLeadTime }) : '-' }}
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('restocking.recommendedItems') }} ({{ plan.items.length }})</h3>
          <button
            class="place-order-btn"
            :disabled="!plan.items.length || placing || refreshing"
            @click="placeOrder"
          >
            {{ placing ? t('restocking.placing') : t('restocking.placeOrder') }}
          </button>
        </div>

        <div v-if="confirmation" class="notice success-notice" role="status">
          {{ t('restocking.orderPlaced', { number: confirmation.order_number, days: confirmation.lead_time_days }) }}
          <router-link to="/orders">{{ t('restocking.viewInOrders') }}</router-link>
        </div>
        <div v-if="orderError" class="notice error-notice" role="alert">{{ orderError }}</div>

        <div v-if="!plan.items.length" class="empty-state">{{ t('restocking.noRecommendations') }}</div>
        <div v-else class="table-container">
          <table class="restock-table">
            <thead>
              <tr>
                <th>{{ t('restocking.table.item') }}</th>
                <th>{{ t('restocking.table.trend') }}</th>
                <th class="num">{{ t('restocking.table.demandGap') }}</th>
                <th class="num">{{ t('restocking.table.quantity') }}</th>
                <th class="num">{{ t('restocking.table.unitCost') }}</th>
                <th class="num">{{ t('restocking.table.lineTotal') }}</th>
                <th class="num">{{ t('restocking.table.leadTime') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in plan.items" :key="item.sku">
                <td>
                  <strong>{{ translateProductName(item.name) }}</strong>
                  <div class="sku">{{ item.sku }}</div>
                </td>
                <td><span :class="['badge', item.trend]">{{ t(`trends.${item.trend}`) }}</span></td>
                <td class="num">{{ item.demand_gap }}</td>
                <td class="num">
                  {{ item.quantity }}
                  <span v-if="item.quantity < item.demand_gap" class="partial">/ {{ item.demand_gap }}</span>
                </td>
                <td class="num">{{ formatMoney(item.unit_cost) }}</td>
                <td class="num"><strong>{{ formatMoney(item.line_total) }}</strong></td>
                <td class="num">{{ t('restocking.leadTimeDays', { days: item.lead_time_days }) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'
import { api } from '../api'
import { useI18n } from '../composables/useI18n'
import { formatCurrencyWithDecimals } from '../utils/currency'

export default {
  name: 'Restocking',
  setup() {
    const { t, currentCurrency, translateProductName } = useI18n()

    const loading = ref(true)
    const error = ref(null)
    const refreshing = ref(false)
    const placing = ref(false)
    const orderError = ref(null)
    const confirmation = ref(null)

    const budget = ref(0)
    const fullRestockCost = ref(0)
    const plan = ref({ total_cost: 0, remaining_budget: 0, items: [] })

    // Let the slider go a bit past the full restock cost so "fund everything" is reachable
    const sliderMax = computed(() => Math.ceil((fullRestockCost.value * 1.2) / 100) * 100)
    const sliderStep = computed(() => Math.max(10, Math.round(sliderMax.value / 200 / 10) * 10))

    // An order arrives when its slowest item does
    const orderLeadTime = computed(() =>
      Math.max(0, ...plan.value.items.map(item => item.lead_time_days))
    )

    const formatMoney = (amount) => formatCurrencyWithDecimals(amount, currentCurrency.value, 2)

    // Ignore responses from superseded requests so a slow reply can't overwrite a newer budget
    let latestRequest = 0
    const loadPlan = async () => {
      const requestId = ++latestRequest
      try {
        refreshing.value = true
        const result = await api.getRestockingRecommendations(budget.value)
        if (requestId === latestRequest) {
          plan.value = result
        }
      } catch (err) {
        error.value = 'Failed to load recommendations: ' + err.message
      } finally {
        if (requestId === latestRequest) {
          refreshing.value = false
        }
      }
    }

    // Debounce slider drags so we don't send a request per pixel
    let debounceTimer = null
    watch(budget, () => {
      confirmation.value = null
      orderError.value = null
      clearTimeout(debounceTimer)
      debounceTimer = setTimeout(loadPlan, 200)
    })

    const placeOrder = async () => {
      try {
        placing.value = true
        orderError.value = null
        confirmation.value = await api.placeRestockingOrder({
          budget: budget.value,
          items: plan.value.items.map(item => ({ sku: item.sku, quantity: item.quantity }))
        })
      } catch (err) {
        orderError.value = `${t('restocking.orderFailed')}: ${err.response?.data?.detail || err.message}`
      } finally {
        placing.value = false
      }
    }

    onMounted(async () => {
      try {
        // A zero-budget request returns the full restock cost, which sizes the slider
        const initial = await api.getRestockingRecommendations(0)
        fullRestockCost.value = initial.full_restock_cost
        budget.value = Math.round(initial.full_restock_cost / 2 / 100) * 100
        await loadPlan()
      } catch (err) {
        error.value = 'Failed to load recommendations: ' + err.message
      } finally {
        loading.value = false
      }
    })

    onBeforeUnmount(() => clearTimeout(debounceTimer))

    return {
      t,
      loading,
      error,
      refreshing,
      placing,
      orderError,
      confirmation,
      budget,
      fullRestockCost,
      plan,
      sliderMax,
      sliderStep,
      orderLeadTime,
      formatMoney,
      placeOrder,
      translateProductName
    }
  }
}
</script>

<style scoped>
.budget-card {
  margin-bottom: 1.5rem;
}

.budget-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  margin-bottom: 0.75rem;
}

.budget-label {
  font-size: 0.875rem;
  font-weight: 600;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.budget-value {
  font-size: 1.75rem;
  font-weight: 700;
  color: #0f172a;
}

.budget-slider {
  width: 100%;
  accent-color: #0f172a;
  cursor: pointer;
}

.budget-scale {
  display: flex;
  justify-content: space-between;
  margin-top: 0.5rem;
  font-size: 0.813rem;
  color: #64748b;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.place-order-btn {
  background: #0f172a;
  color: white;
  border: none;
  border-radius: 8px;
  padding: 0.625rem 1.25rem;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}

.place-order-btn:hover:not(:disabled) {
  background: #334155;
}

.place-order-btn:disabled {
  background: #cbd5e1;
  cursor: not-allowed;
}

.notice {
  margin: 0 0 1rem;
  padding: 0.75rem 1rem;
  border-radius: 8px;
  font-size: 0.875rem;
}

.notice a {
  margin-left: 0.5rem;
  font-weight: 600;
  color: inherit;
}

.success-notice {
  background: #f0fdf4;
  color: #166534;
  border: 1px solid #bbf7d0;
}

.error-notice {
  background: #fef2f2;
  color: #991b1b;
  border: 1px solid #fecaca;
}

.empty-state {
  padding: 2rem;
  text-align: center;
  color: #64748b;
}

.restock-table .num {
  text-align: right;
}

.sku {
  font-size: 0.75rem;
  color: #64748b;
}

.partial {
  font-size: 0.75rem;
  color: #d97706;
}
</style>
