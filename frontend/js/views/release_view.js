export const releaseViewTemplate = `
<section class="panel">
  <h2>解笼拨付</h2>
  <p class="hint">仅核验通过/部分解笼的笼可拨付。普通拨付默认全额、可部分；<b>尾款清尾</b>结清部分解笼后剩余的全部可用额，同事务扣笼可用、减账户余额与冻结并记审计。</p>
  <div class="grid-2">
    <div>
      <div class="form-row">
        <div>
          <label>通过态笼（含部分解笼）</label>
          <select v-model.number="releaseForm.cage_id">
            <option :value="0">请选择</option>
            <option v-for="c in passCages" :key="c.id" :value="c.id">
              {{ c.cage_code }} · {{ c.status }} · 可用 {{ fmt(c.available) }}
            </option>
          </select>
        </div>
        <div>
          <label>金额（空=全额；尾款清尾时忽略）</label>
          <input type="number" v-model.number="releaseForm.amount" min="0" step="1000" placeholder="全额"/>
        </div>
        <div>
          <label>收款方（空=受益人）</label>
          <input v-model="releaseForm.payee"/>
        </div>
      </div>
      <div class="actions">
        <button class="ok" @click="doRelease">解笼拨付</button>
        <button class="ok" @click="doTailRelease">尾款清尾</button>
      </div>
      <p class="hint" v-if="selectedReleaseCage">
        当前笼 {{ selectedReleaseCage.cage_code }}：状态 {{ selectedReleaseCage.status }}，
        可用 {{ fmt(selectedReleaseCage.available) }} / 总额 {{ fmt(selectedReleaseCage.amount) }}
      </p>
    </div>
    <div>
      <table>
        <thead>
          <tr><th>凭证</th><th>笼</th><th>金额</th><th>收款方</th><th>笼代</th><th>类型</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in releases" :key="r.id">
            <td>{{ r.release_ref }}</td>
            <td>{{ r.cage_code }}</td>
            <td>{{ fmt(r.amount) }}</td>
            <td>{{ r.payee }}</td>
            <td>{{ r.cage_generation }}</td>
            <td>
              <span class="badge" :class="r.is_tail ? 'pass' : 'done'">
                {{ r.is_tail ? '尾款' : '拨付' }}
              </span>
            </td>
            <td>{{ r.created_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</section>
`;
