<!-- 401 引导页（§6.3：统一 401 拦截后跳转；禁止失效态发起写操作） -->
<template>
  <div class="card" style="text-align:center;padding:48px 24px">
    <h2 style="font-size:18px">会话已失效</h2>
    <p style="line-height:1.8;color:#556">
      请从系统托盘右键菜单「打开控制台」重新进入。<br />
      托盘会自动换取一次性会话；若托盘未运行，请先启动 SAU Agent 托盘。<br />
      若刚从托盘重新打开控制台，旧标签页会被新会话顶替，也属正常现象。
    </p>
  </div>
</template>
