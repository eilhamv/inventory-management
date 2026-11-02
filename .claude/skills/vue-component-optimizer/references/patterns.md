# Vue 3 optimization patterns

Before/after reference for the findings this skill reports. Read only the sections you need.

## Contents
1. Render-time work → `computed`
2. Stable keys in `v-for`
3. Filtering in templates (`v-for` + `v-if`)
4. Composables at setup top level
5. Cached formatters
6. Lazy routes and async components
7. Splitting oversized components
8. Extracting shared logic (composables and utils)
9. Watchers
10. Large lists
11. Static data out of reactive state

---

## 1. Render-time work → `computed`

A function called from the template runs on **every** render of that component, once per call site. `computed` caches until its reactive dependencies change.

```vue
<!-- Before: filters all orders 4x per render -->
<div class="stat-value">{{ getOrdersByStatus('Delivered').length }}</div>
<div class="stat-value">{{ getOrdersByStatus('Shipped').length }}</div>
```
```js
// After: one pass, cached
const statusCounts = computed(() => {
  const counts = {}
  for (const order of orders.value) counts[order.status] = (counts[order.status] || 0) + 1
  return counts
})
```
```vue
<div class="stat-value">{{ statusCounts.Delivered || 0 }}</div>
```

Template calls are fine when they're cheap and pure: a lookup in a small map, or `t('key')`. Flag them only when they iterate collections, build objects or arrays, or format with new `Intl` objects.

For per-item calls inside `v-for` (e.g. `getStatusClass(item.status)`), either precompute a decorated list in a `computed`, or make sure the function is an O(1) lookup.

## 2. Stable keys in `v-for`

`:key="index"` makes Vue match DOM and child state by position. Sorting, filtering or inserting then reuses the wrong elements. Inputs keep the wrong values, and `<details>` stays open on the wrong row. Use a stable id from the data (`id`, `sku`, `order_number`). If items truly have no identity, use a composite like `` `${item.sku}-${item.month}` ``.

Check the data before recommending a key: a field that looks unique (an `id`, a `sku`) can still repeat within one list, and a duplicate key triggers Vue warnings and the same wrong reuse. If nothing is unique, `` `${item.sku}-${idx}` `` is acceptable for lists that never reorder.

## 3. Filtering in templates

`v-if` on the same element as `v-for` re-evaluates for every item on every render, and in Vue 3 `v-if` has higher priority, which makes the loop variable unavailable to it. Filter in a `computed` and loop over the result.

## 4. Composables at setup top level

Composables such as `useI18n()` or `useFilters()` must be called synchronously at the top level of `setup()`:
- Calling one inside a helper (`const formatDate = () => { const { currentLocale } = useI18n() ... }`) runs the composable's body on every call, which may register watchers or create refs each time.
- After an `await`, the current component instance is lost, so composables that use `inject`, lifecycle hooks or `getCurrentInstance` break.

Hoist the call and close over the result:

```js
// Before
const formatDate = (d) => {
  const { currentLocale } = useI18n()
  return new Date(d).toLocaleDateString(currentLocale.value === 'ja' ? 'ja-JP' : 'en-US', opts)
}
// After
const { currentLocale } = useI18n()
const formatDate = (d) => new Date(d).toLocaleDateString(currentLocale.value === 'ja' ? 'ja-JP' : 'en-US', opts)
```

## 5. Cached formatters

`toLocaleString` and `toLocaleDateString` with options construct an `Intl` formatter on every call. In tables, that's per cell per render. Create the formatter once per locale:

```js
// utils/format.js
const cache = new Map()
export function dateFormatter(locale, options) {
  const key = locale + JSON.stringify(options)
  if (!cache.has(key)) cache.set(key, new Intl.DateTimeFormat(locale, options))
  return cache.get(key)
}
```

## 6. Lazy routes and async components

Statically imported route views all land in the initial bundle. Use dynamic imports so each view becomes its own chunk:

```js
// Before
import Reports from './views/Reports.vue'
{ path: '/reports', component: Reports }
// After
{ path: '/reports', component: () => import('./views/Reports.vue') }
```

Keep the landing view (`/`) eager if first paint matters most.

Load modals, drawers and heavy charts that render behind a `v-if` with `defineAsyncComponent`:

```js
import { defineAsyncComponent } from 'vue'
const ProductDetailModal = defineAsyncComponent(() => import('../components/ProductDetailModal.vue'))
```

Rate this Medium or Low unless the bundle is large. Measure with `npx vite build` and compare chunk sizes.

## 7. Splitting oversized components

Signs that a component should be split:
- the template has several independent sections, each with its own data
- the script has several unrelated groups of refs and functions
- the style block is longer than the template

Split along **data ownership**: each child gets the props it renders and emits events upward. Move shared state into a composable rather than prop-drilling it through three levels. Propose concrete child names and their props/emits, e.g. `KpiGrid(summary)`, `OrdersByMonthChart(orders)`.

A long style block is often duplicated card, table or badge CSS. That belongs in shared global styles, not in each component.

## 8. Extracting shared logic

| Shape of the duplicate | Home |
|---|---|
| Pure function (formatting, math, mapping) | `utils/<topic>.js` |
| Uses refs, `computed`, the locale, or other composables | `composables/use<Topic>.js` |
| Repeated fetch + loading + error triple | `composables/useAsyncData.js` (or the project's equivalent) |
| Repeated markup + style (badges, stat cards, modal shells) | a small presentational component |

Before extracting, diff the copies. If they differ only in options, such as date formats, parameterize them. If they differ in behaviour, say so and keep them separate. List every call site that would change.

## 9. Watchers

- Watching four filter refs and reloading **all** data on any change: reload only what depends on the filter that changed. Better still, have the views derive what they show from one cached fetch where the API allows it.
- `deep: true` on large arrays or objects walks the whole structure on each change. Watch a specific getter (`() => obj.value.field`) instead.
- Debounce watchers driven by sliders or text input.

## 10. Large lists

Rendering hundreds of rows with nested `<details>` or computed classes per row is costly on every filter change. Options, from cheapest up: paginate, use `v-memo` on rows keyed by the fields they display, or virtualize (e.g. `vue-virtual-scroller`). Recommend virtualization only for 500+ rows that are actually visible at once.

## 11. Static data out of reactive state

Lookup tables, chart colours, option lists and status-to-class maps that never change shouldn't live in `ref()`/`reactive()` or be rebuilt inside `setup()`. Declare them as module-level constants. If one must be in reactive state, use `Object.freeze` or `markRaw` for large immutable data.
