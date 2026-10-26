# Evaluator containment deny-handler suite

```sh
deno test -A tools/evaluator_containment_handler_suite/test_deny_handlers.ts
```

Does not bind ports (strips `Deno.serve` before evaluating `containedResponse`).
