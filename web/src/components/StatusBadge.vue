<script setup lang="ts">
defineProps<{ tone: "ok" | "warn" | "error" | "muted"; label: string }>();
</script>

<template><span class="status-badge" :class="`is-${tone}`"><i></i>{{ label }}</span></template>
