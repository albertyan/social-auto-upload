<!-- 升级页：调 GET /upgrade（当前占位 501，升级编排 §7.4 属 S7）→
     优雅展示「升级功能建设中」 -->
<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, UnauthorizedError } from '../api.js'

const building = ref(false)
const info = ref(null)
const err = ref('')

onMounted(async () => {
  try {
    info.value = await apiGet('/upgrade')
  } catch (e) {
    if (e instanceof UnauthorizedError) return
    if (e.status === 501) building.value = true
    else err.value = e.message
  }
})
</script>

<template>
  <div class="card">
    <h2>升级</h2>
    <p v-if="building" style="color:#7f8c9b">
      升级功能建设中：当前版本将通过安装包整体升级，自动升级编排将在后续版本上线。
    </p>
    <p v-else-if="err" class="msg err">{{ err }}</p>
    <table v-else-if="info">
      <tr v-for="(v, k) in info" :key="k">
        <th>{{ k }}</th>
        <td>{{ v }}</td>
      </tr>
    </table>
    <p v-else style="color:#7f8c9b">加载中…</p>
  </div>
</template>
