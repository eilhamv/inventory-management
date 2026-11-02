---
type: llm
focus: trace
weight: 1
---

report.md handles the :key="idx" on the Orders.vue order items dropdown correctly: since orders can repeat a SKU, it must not recommend a plain item.sku key (keeping idx with a reason, or a composite/unique key, both pass)
