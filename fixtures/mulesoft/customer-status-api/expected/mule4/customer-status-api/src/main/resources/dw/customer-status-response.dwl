%dw 2.0
output application/json
---
{
  customerId: vars.customerId,
  status: "ACTIVE",
  source: "synthetic-fixture"
}
