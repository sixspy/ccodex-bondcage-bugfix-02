export const auditViewTemplate = `
<section class="panel">
  <h2>审计争用</h2>
  <p class="hint">写动作落审计；后到争用只记 contention，不写业务。</p>
  <p class="hint" style="border-left:3px solid var(--ok);padding-left:8px">
    命运口径（甲路径）：每条 <code>cage_release</code> 成功审计的 release_id 必非空、
    凭证号可在解笼拨付页按同号查到真实册行；守卫对不上时记
    <code>audit_release_yaw</code> 偏航痕并整笔回滚，成功审计零增。
  </p>
  <div class="grid-2">
    <div>
      <h3>审计</h3>
      <table>
        <thead><tr><th>时间</th><th>人</th><th>动作</th><th>目标</th><th>拨付凭证 / 载荷</th></tr></thead>
        <tbody>
          <tr v-for="a in audits" :key="a.id" :class="{ 'row-yaw': a.action==='audit_release_yaw' }">
            <td>{{ a.created_at }}</td>
            <td>{{ a.actor }}</td>
            <td>
              <span class="badge" :class="a.action==='audit_release_yaw' ? 'fail' : (a.action==='cage_release' ? 'pass' : 'done')">{{ a.action }}</span>
            </td>
            <td>{{ a.target }}</td>
            <td>
              <template v-if="parsedPayload(a).release_ref">
                <code>{{ parsedPayload(a).release_ref }}</code>
                <span class="hint"> · 册行 #{{ parsedPayload(a).release_id }}</span>
              </template>
              <code v-else style="font-size:11px">{{ a.payload }}</code>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div>
      <h3>持锁</h3>
      <table>
        <thead><tr><th>键</th><th>持有者</th><th>时间</th></tr></thead>
        <tbody>
          <tr v-for="l in locks" :key="l.lock_key">
            <td>{{ l.lock_key }}</td>
            <td>{{ l.holder }}</td>
            <td>{{ l.acquired_at }}</td>
          </tr>
        </tbody>
      </table>
      <h3 style="margin-top:14px">争用</h3>
      <table>
        <thead><tr><th>时间</th><th>键</th><th>人</th><th>动作</th><th>详情</th></tr></thead>
        <tbody>
          <tr v-for="c in contention" :key="c.id">
            <td>{{ c.created_at }}</td>
            <td>{{ c.lock_key }}</td>
            <td>{{ c.actor }}</td>
            <td>{{ c.action }}</td>
            <td>{{ c.detail }}</td>
          </tr>
          <tr v-if="!contention.length"><td colspan="5" class="hint">暂无争用留痕</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</section>
`;
