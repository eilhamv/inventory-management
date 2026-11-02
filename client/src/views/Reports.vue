<template>
  <div class="reports">
    <div class="page-header">
      <h2>{{ t('reports.title') }}</h2>
      <p>{{ t('reports.description') }}</p>
    </div>

    <div v-if="loading" class="loading">{{ t('common.loading') }}</div>
    <div v-else-if="error" class="error">{{ error }}</div>
    <div v-else-if="!monthlyData.length" class="card empty-state">{{ t('reports.noData') }}</div>
    <div v-else>
      <div class="stats-grid">
        <div class="stat-card info">
          <div class="stat-label">{{ t('reports.stats.totalRevenue') }}</div>
          <div class="stat-value">{{ formatMoney(totalRevenue) }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('reports.stats.avgMonthlyRevenue') }}</div>
          <div class="stat-value">{{ formatMoney(avgMonthlyRevenue) }}</div>
        </div>
        <div class="stat-card info">
          <div class="stat-label">{{ t('reports.stats.totalOrders') }}</div>
          <div class="stat-value">{{ totalOrders.toLocaleString() }}</div>
        </div>
        <div class="stat-card success">
          <div class="stat-label">{{ t('reports.stats.bestQuarter') }}</div>
          <div class="stat-value">{{ bestQuarter || '-' }}</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('reports.quarterlyPerformance') }}</h3>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('reports.table.quarter') }}</th>
                <th class="num">{{ t('reports.table.totalOrders') }}</th>
                <th class="num">{{ t('reports.table.totalRevenue') }}</th>
                <th class="num">{{ t('reports.table.avgOrderValue') }}</th>
                <th class="num">{{ t('reports.table.fulfillmentRate') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="q in quarterlyData" :key="q.quarter">
                <td><strong>{{ q.quarter }}</strong></td>
                <td class="num">{{ q.total_orders }}</td>
                <td class="num">{{ formatMoney(q.total_revenue) }}</td>
                <td class="num">{{ formatMoney(q.avg_order_value) }}</td>
                <td class="num">
                  <span :class="['badge', fulfillmentClass(q.fulfillment_rate)]">{{ q.fulfillment_rate }}%</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('reports.monthlyRevenueTrend') }}</h3>
        </div>
        <div class="bar-chart" role="img" :aria-label="t('reports.monthlyRevenueTrend')">
          <div v-for="row in monthlyRows" :key="row.month" class="bar-wrapper">
            <div class="bar-container">
              <div class="bar" :style="{ height: row.barHeight + '%' }" :title="`${row.label}: ${formatMoney(row.revenue)}`"></div>
            </div>
            <div class="bar-label">{{ row.label }}</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <h3 class="card-title">{{ t('reports.monthOverMonth') }}</h3>
        </div>
        <div class="table-container">
          <table>
            <thead>
              <tr>
                <th>{{ t('reports.table.month') }}</th>
                <th class="num">{{ t('reports.table.orders') }}</th>
                <th class="num">{{ t('reports.table.revenue') }}</th>
                <th class="num">{{ t('reports.table.change') }}</th>
                <th class="num">{{ t('reports.table.growthRate') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in monthlyRows" :key="row.month">
                <td><strong>{{ row.label }}</strong></td>
                <td class="num">{{ row.order_count }}</td>
                <td class="num">{{ formatMoney(row.revenue) }}</td>
                <td class="num">
                  <span v-if="row.change !== null" :class="changeClass(row.change)">{{ formatChange(row.change) }}</span>
                  <span v-else>-</span>
                </td>
                <td class="num">
                  <span v-if="row.change !== null" :class="changeClass(row.change)">{{ formatGrowth(row.growthRate) }}</span>
                  <span v-else>-</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, computed, watch, onMounted } from 'vue'
import { api } from '../api'
import { useFilters } from '../composables/useFilters'
import { useI18n } from '../composables/useI18n'
import { formatCurrency } from '../utils/currency'

const MONTH_KEYS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']

export default {
  name: 'Reports',
  setup() {
    const { t, currentLocale, currentCurrency } = useI18n()
    const {
      selectedPeriod,
      selectedLocation,
      selectedCategory,
      selectedStatus,
      getCurrentFilters
    } = useFilters()

    const loading = ref(true)
    const error = ref(null)
    const quarterlyData = ref([])
    const monthlyData = ref([])

    // Ignore responses from superseded requests so fast filter changes can't show stale data
    let latestRequest = 0
    const loadReports = async () => {
      const requestId = ++latestRequest
      try {
        error.value = null
        const filters = getCurrentFilters()
        const [quarterly, monthly] = await Promise.all([
          api.getQuarterlyReports(filters),
          api.getMonthlyTrends(filters)
        ])
        if (requestId === latestRequest) {
          quarterlyData.value = quarterly
          monthlyData.value = monthly
        }
      } catch (err) {
        if (requestId === latestRequest) {
          error.value = `${t('reports.loadError')}: ${err.message}`
        }
      } finally {
        if (requestId === latestRequest) {
          loading.value = false
        }
      }
    }

    watch([selectedPeriod, selectedLocation, selectedCategory, selectedStatus], loadReports)
    onMounted(loadReports)

    const formatMoney = (amount) => formatCurrency(amount, currentCurrency.value)

    const formatMonth = (monthStr) => {
      const [year, month] = monthStr.split('-')
      const name = t(`months.${MONTH_KEYS[parseInt(month, 10) - 1]}`)
      return currentLocale.value === 'ja' ? `${year}年${name}` : `${name} ${year}`
    }

    // Derived once per data or locale change instead of on every render
    const monthlyRows = computed(() => {
      const maxRevenue = Math.max(0, ...monthlyData.value.map(m => m.revenue))
      return monthlyData.value.map((m, i) => {
        const previous = i > 0 ? monthlyData.value[i - 1].revenue : null
        return {
          ...m,
          label: formatMonth(m.month),
          barHeight: maxRevenue ? (m.revenue / maxRevenue) * 100 : 0,
          change: previous === null ? null : m.revenue - previous,
          growthRate: previous ? ((m.revenue - previous) / previous) * 100 : null
        }
      })
    })

    const totalRevenue = computed(() => monthlyData.value.reduce((sum, m) => sum + m.revenue, 0))
    const totalOrders = computed(() => monthlyData.value.reduce((sum, m) => sum + m.order_count, 0))
    const avgMonthlyRevenue = computed(() =>
      monthlyData.value.length ? totalRevenue.value / monthlyData.value.length : 0
    )
    const bestQuarter = computed(() => {
      const best = quarterlyData.value.reduce(
        (top, q) => (!top || q.total_revenue > top.total_revenue ? q : top), null
      )
      return best ? best.quarter : ''
    })

    const fulfillmentClass = (rate) => {
      if (rate >= 90) return 'success'
      if (rate >= 75) return 'warning'
      return 'danger'
    }

    const changeClass = (change) => {
      if (change > 0) return 'positive-change'
      if (change < 0) return 'negative-change'
      return ''
    }

    const formatChange = (change) => {
      const sign = change > 0 ? '+' : change < 0 ? '-' : ''
      return sign + formatMoney(Math.abs(change))
    }

    const formatGrowth = (rate) => {
      if (rate === null) return t('reports.notApplicable')
      return (rate > 0 ? '+' : '') + rate.toFixed(1) + '%'
    }

    return {
      t,
      loading,
      error,
      quarterlyData,
      monthlyData,
      monthlyRows,
      totalRevenue,
      totalOrders,
      avgMonthlyRevenue,
      bestQuarter,
      formatMoney,
      fulfillmentClass,
      changeClass,
      formatChange,
      formatGrowth
    }
  }
}
</script>

<style scoped>
.num {
  text-align: right;
}

.empty-state {
  text-align: center;
  color: #64748b;
  padding: 2rem;
}

.bar-chart {
  display: flex;
  align-items: flex-end;
  justify-content: space-around;
  gap: 0.5rem;
  padding: 1rem 0.5rem 0;
}

.bar-wrapper {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  max-width: 80px;
}

.bar-container {
  height: 200px;
  display: flex;
  align-items: flex-end;
  width: 100%;
}

.bar {
  width: 100%;
  background: linear-gradient(to top, #3b82f6, #60a5fa);
  border-radius: 4px 4px 0 0;
  transition: height 0.3s;
}

.bar:hover {
  background: linear-gradient(to top, #2563eb, #3b82f6);
}

.bar-label {
  margin-top: 1.5rem;
  font-size: 0.75rem;
  color: #64748b;
  transform: rotate(-45deg);
  white-space: nowrap;
}

.positive-change {
  color: #16a34a;
  font-weight: 600;
}

.negative-change {
  color: #dc2626;
  font-weight: 600;
}
</style>
