(function () {
  const defaultConfig = {
    title: "冷水机组",
    modelLabel: "设备型号",
    selectedModel: "CH-150",
    quantityLabel: "设备数量",
    quantity: 2,
    minQuantity: 1,
    maxQuantity: 9,
    rows: [
      { label: "设备型号", type: "model" },
      { label: "额定制冷量 (kW)", field: "capacity" },
      { label: "额定输入功率 (kW)", field: "power" },
      { label: "性能系数 COP", field: "cop" },
      { label: "蒸发器压降 (kPa)", field: "evaporatorDrop" },
      { label: "冷凝器压降 (kPa)", field: "condenserDrop" },
      { label: "冷冻水进 / 出水温度 (℃)", value: "12 / 7" },
      { label: "冷却水进 / 出水温度 (℃)", value: "30 / 35" }
    ],
    models: [
      {
        value: "CH-150",
        label: "CH-150",
        fields: {
          capacity: "527.50",
          power: "101.40",
          cop: "5.20",
          evaporatorDrop: "45",
          condenserDrop: "52"
        }
      },
      {
        value: "CH-180",
        label: "CH-180",
        fields: {
          capacity: "633.00",
          power: "119.40",
          cop: "5.30",
          evaporatorDrop: "48",
          condenserDrop: "55"
        }
      }
    ]
  };

  const styles = `
    :host {
      --condition-panel: #0b2c52;
      --condition-panel-strong: #0d3764;
      --condition-line: #167a9a;
      --condition-line-soft: rgba(32, 166, 193, 0.26);
      --condition-accent: #27c1c8;
      --condition-text: #eefbff;
      --condition-muted: #a9c4d5;
      --condition-control: #0c7181;
      display: block;
      min-width: 0;
      color: var(--condition-text);
      font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    button,
    select {
      font: inherit;
    }

    button:focus-visible,
    select:focus-visible {
      outline: 2px solid #8cf5f1;
      outline-offset: 2px;
    }

    .card {
      position: relative;
      display: flex;
      height: 100%;
      min-width: 0;
      min-height: 580px;
      flex-direction: column;
      padding: 0 24px 20px;
      border: 1px solid var(--condition-line);
      background: var(--condition-panel);
    }

    .card::before,
    .card::after {
      position: absolute;
      top: -1px;
      width: 8px;
      height: 8px;
      border-top: 2px solid var(--condition-accent);
      content: "";
    }

    .card::before {
      left: -1px;
      border-left: 2px solid var(--condition-accent);
    }

    .card::after {
      right: -1px;
      border-right: 2px solid var(--condition-accent);
    }

    .title {
      margin: 0 -24px 18px;
      padding: 16px 20px 15px;
      border-bottom: 1px solid var(--condition-line-soft);
      background: var(--condition-panel-strong);
      text-align: center;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0;
    }

    .parameter-list {
      display: grid;
      gap: 0;
      margin: 0;
    }

    .parameter-row {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(140px, 0.9fr);
      align-items: center;
      gap: 12px;
      min-height: 43px;
      border-bottom: 1px solid rgba(139, 208, 224, 0.08);
    }

    .operating-modes {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 10px 0 4px;
    }

    .operating-mode {
      min-width: 0;
      background: rgba(17, 68, 119, 0.42);
    }

    .mode-title {
      margin: 0;
      padding: 8px 6px;
      border-bottom: 1px solid var(--condition-line-soft);
      text-align: center;
      font-size: 13px;
      font-weight: 700;
    }

    .mode-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      align-items: center;
      gap: 6px;
      min-height: 40px;
      padding: 5px 8px;
      border-bottom: 1px solid rgba(139, 208, 224, 0.08);
    }

    .mode-label {
      min-width: 0;
      color: #c6dbe6;
      font-size: 12px;
      line-height: 1.4;
    }

    .mode-value {
      color: var(--condition-accent);
      font-family: "Bahnschrift", "Microsoft YaHei", sans-serif;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }

    dt {
      min-width: 0;
      color: #c6dbe6;
      font-size: 13px;
      line-height: 1.45;
    }

    dd {
      min-width: 0;
      margin: 0;
      color: var(--condition-accent);
      font-family: "Bahnschrift", "Microsoft YaHei", sans-serif;
      font-size: 14px;
      font-variant-numeric: tabular-nums;
      text-align: right;
      overflow-wrap: anywhere;
    }

    .model-select {
      width: 100%;
      min-height: 34px;
      padding: 0 30px 0 10px;
      border: 1px solid rgba(56, 211, 211, 0.2);
      border-radius: 2px;
      background: var(--condition-control);
      color: #eaffff;
      cursor: pointer;
    }

    .quantity-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-top: auto;
      padding-top: 24px;
    }

    .quantity-label {
      color: #c6dbe6;
      font-size: 13px;
    }

    .stepper {
      display: grid;
      grid-template-columns: 34px 54px 34px;
      height: 34px;
      border: 1px solid rgba(48, 181, 202, 0.25);
    }

    .stepper button,
    .stepper output {
      display: grid;
      place-items: center;
      border: 0;
      background: #114477;
      color: var(--condition-muted);
    }

    .stepper button {
      min-width: 34px;
      cursor: pointer;
      font-size: 18px;
    }

    .stepper button:hover {
      background: #155788;
      color: #fff;
    }

    .stepper output {
      border-right: 1px solid rgba(48, 181, 202, 0.25);
      border-left: 1px solid rgba(48, 181, 202, 0.25);
      color: var(--condition-accent);
      font-family: "Bahnschrift", sans-serif;
      font-size: 15px;
    }

    @media (max-width: 420px) {
      .card {
        min-height: 550px;
        padding-right: 16px;
        padding-left: 16px;
      }

      .title {
        margin-right: -16px;
        margin-left: -16px;
      }

      .parameter-row {
        grid-template-columns: minmax(0, 1fr) minmax(126px, 0.8fr);
      }
    }
  `;

  function cloneConfig(config) {
    return JSON.parse(JSON.stringify(config));
  }

  class BasicConditionCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._config = cloneConfig(defaultConfig);
    }

    connectedCallback() {
      this.render();
    }

    set config(value) {
      this._config = cloneConfig({ ...defaultConfig, ...value });
      if (this.isConnected) this.render();
    }

    get config() {
      return cloneConfig(this._config);
    }

    get selectedModel() {
      return this._config.selectedModel;
    }

    get quantity() {
      return this._config.quantity;
    }

    render() {
      const config = this._config;
      const activeModel = config.models.find((model) => model.value === config.selectedModel) || config.models[0];
      config.selectedModel = activeModel?.value || "";

      this.shadowRoot.innerHTML = `
        <style>${styles}</style>
        <article class="card">
          <h2 class="title"></h2>
          <dl class="parameter-list"></dl>
          <div class="quantity-row">
            <span class="quantity-label"></span>
            <div class="stepper">
              <button type="button" data-action="decrease">−</button>
              <output aria-live="polite"></output>
              <button type="button" data-action="increase">+</button>
            </div>
          </div>
        </article>
      `;

      this.shadowRoot.querySelector(".title").textContent = config.title;
      this.shadowRoot.querySelector(".quantity-label").textContent = config.quantityLabel;

      const parameterList = this.shadowRoot.querySelector(".parameter-list");
      config.rows.forEach((row) => {
        if (row.type === "operatingModes") {
          const modes = document.createElement("div");
          modes.className = "operating-modes";

          row.modes.forEach((mode) => {
            const modeSection = document.createElement("section");
            modeSection.className = "operating-mode";
            const modeTitle = document.createElement("h3");
            modeTitle.className = "mode-title";
            modeTitle.textContent = mode.title;
            modeSection.append(modeTitle);

            mode.rows.forEach((modeRow) => {
              const item = document.createElement("div");
              item.className = "mode-row";
              const itemLabel = document.createElement("span");
              itemLabel.className = "mode-label";
              itemLabel.textContent = modeRow.label;
              const itemValue = document.createElement("strong");
              itemValue.className = "mode-value";
              itemValue.textContent = modeRow.field ? activeModel?.fields[modeRow.field] ?? "-" : modeRow.value;
              if (modeRow.field) itemValue.dataset.field = modeRow.field;
              item.append(itemLabel, itemValue);
              modeSection.append(item);
            });

            modes.append(modeSection);
          });

          parameterList.append(modes);
          return;
        }

        const wrapper = document.createElement("div");
        wrapper.className = "parameter-row";

        const label = document.createElement("dt");
        label.textContent = row.label;
        const value = document.createElement("dd");

        if (row.type === "model") {
          const select = document.createElement("select");
          select.className = "model-select";
          select.setAttribute("aria-label", `${config.title}${config.modelLabel}`);
          config.models.forEach((model) => {
            const option = document.createElement("option");
            option.value = model.value;
            option.textContent = model.label;
            option.selected = model.value === config.selectedModel;
            select.append(option);
          });
          value.append(select);
        } else {
          value.textContent = row.field ? activeModel?.fields[row.field] ?? "-" : row.value;
          if (row.field) value.dataset.field = row.field;
        }

        wrapper.append(label, value);
        parameterList.append(wrapper);
      });

      const stepper = this.shadowRoot.querySelector(".stepper");
      const output = stepper.querySelector("output");
      stepper.setAttribute("aria-label", `${config.title}${config.quantityLabel}`);
      stepper.querySelector('[data-action="decrease"]').setAttribute("aria-label", `减少${config.title}数量`);
      stepper.querySelector('[data-action="increase"]').setAttribute("aria-label", `增加${config.title}数量`);
      output.value = config.quantity;
      output.textContent = config.quantity;

      this.bindEvents();
    }

    bindEvents() {
      const select = this.shadowRoot.querySelector(".model-select");
      const stepper = this.shadowRoot.querySelector(".stepper");
      const output = stepper.querySelector("output");

      select?.addEventListener("change", () => {
        this._config.selectedModel = select.value;
        const activeModel = this._config.models.find((model) => model.value === select.value);

        Object.entries(activeModel?.fields || {}).forEach(([field, value]) => {
          const target = this.shadowRoot.querySelector(`[data-field="${field}"]`);
          if (target) target.textContent = value;
        });

        this.dispatchEvent(new CustomEvent("model-change", {
          bubbles: true,
          detail: { model: select.value, fields: cloneConfig(activeModel?.fields || {}) }
        }));
      });

      stepper.addEventListener("click", (event) => {
        const button = event.target.closest("button");
        if (!button) return;

        const delta = button.dataset.action === "increase" ? 1 : -1;
        const next = Math.min(
          this._config.maxQuantity,
          Math.max(this._config.minQuantity, this._config.quantity + delta)
        );

        if (next === this._config.quantity) return;
        this._config.quantity = next;
        output.value = next;
        output.textContent = next;
        this.dispatchEvent(new CustomEvent("quantity-change", {
          bubbles: true,
          detail: { quantity: next }
        }));
      });
    }
  }

  if (!customElements.get("basic-condition-card")) {
    customElements.define("basic-condition-card", BasicConditionCard);
  }

  window.BasicConditionCard = BasicConditionCard;
  window.basicConditionCardDefaultConfig = cloneConfig(defaultConfig);
})();
