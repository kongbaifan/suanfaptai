(function () {
  window.centralAirEquipmentGroups = [
    {
      title: "冷热源与输配系统",
      description: "主机房设备",
      equipment: [
        {
          title: "冷水机组",
          selectedModel: "CH-150",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            { label: "额定制冷量 (kW)", field: "capacity" },
            { label: "额定输入功率 (kW)", field: "power" },
            { label: "性能系数 COP", field: "cop" },
            { label: "蒸发器压降 (kPa)", field: "evaporatorDrop" },
            { label: "冷凝器压降 (kPa)", field: "condenserDrop" },
            { label: "冷冻水进 / 出水温度 (℃)", value: "12 / 7" },
            { label: "冷却水进 / 出水温度 (℃)", value: "30 / 35" },
            { label: "制冷剂", field: "refrigerant" }
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
                condenserDrop: "52",
                refrigerant: "R134a"
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
                condenserDrop: "55",
                refrigerant: "R134a"
              }
            }
          ]
        },
        {
          title: "地源热泵机组",
          selectedModel: "GSHP-500",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            {
              type: "operatingModes",
              modes: [
                {
                  title: "制冷工况",
                  rows: [
                    { label: "额定制冷量 (kW)", field: "cooling" },
                    { label: "输入功率 (kW)", field: "coolingPower" },
                    { label: "EER", field: "eer" },
                    { label: "蒸发器进 / 出水 (℃)", field: "coolingEvaporator" },
                    { label: "冷凝器进 / 出水 (℃)", field: "coolingCondenser" }
                  ]
                },
                {
                  title: "制热工况",
                  rows: [
                    { label: "额定制热量 (kW)", field: "heating" },
                    { label: "输入功率 (kW)", field: "heatingPower" },
                    { label: "COP", field: "cop" },
                    { label: "蒸发器进 / 出水 (℃)", field: "heatingEvaporator" },
                    { label: "冷凝器进 / 出水 (℃)", field: "heatingCondenser" }
                  ]
                }
              ]
            },
            { label: "蒸发器 / 冷凝器压降 (kPa)", field: "pressureDrop" },
            { label: "设备参考成本 (万元)", field: "cost" }
          ],
          models: [
            {
              value: "GSHP-500",
              label: "GSHP-500",
              fields: {
                cooling: "504.00",
                coolingPower: "76.30",
                eer: "6.61",
                coolingEvaporator: "12 / 7",
                coolingCondenser: "26 / 31",
                heating: "510.00",
                heatingPower: "124.70",
                cop: "4.09",
                heatingEvaporator: "10 / 5",
                heatingCondenser: "40 / 45",
                pressureDrop: "36 / 47",
                cost: "48.92"
              }
            },
            {
              value: "GSHP-650",
              label: "GSHP-650",
              fields: {
                cooling: "650.00",
                coolingPower: "96.30",
                eer: "6.75",
                coolingEvaporator: "12 / 7",
                coolingCondenser: "26 / 31",
                heating: "665.00",
                heatingPower: "158.30",
                cop: "4.20",
                heatingEvaporator: "10 / 5",
                heatingCondenser: "40 / 45",
                pressureDrop: "42 / 53",
                cost: "61.80"
              }
            }
          ]
        },
        {
          title: "水源热泵机组",
          selectedModel: "WSHP-500",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            {
              type: "operatingModes",
              modes: [
                {
                  title: "制冷工况",
                  rows: [
                    { label: "额定制冷量 (kW)", field: "cooling" },
                    { label: "输入功率 (kW)", field: "coolingPower" },
                    { label: "EER", field: "eer" },
                    { label: "冷冻水进 / 出水 (℃)", field: "coolingLoadWater" },
                    { label: "源水进 / 出水 (℃)", field: "coolingSourceWater" }
                  ]
                },
                {
                  title: "制热工况",
                  rows: [
                    { label: "额定制热量 (kW)", field: "heating" },
                    { label: "输入功率 (kW)", field: "heatingPower" },
                    { label: "COP", field: "cop" },
                    { label: "源水进 / 出水 (℃)", field: "heatingSourceWater" },
                    { label: "热水进 / 出水 (℃)", field: "heatingLoadWater" }
                  ]
                }
              ]
            },
            { label: "蒸发器 / 冷凝器压降 (kPa)", field: "pressureDrop" },
            { label: "设备参考成本 (万元)", field: "cost" }
          ],
          models: [
            {
              value: "WSHP-500",
              label: "WSHP-500",
              fields: {
                cooling: "500.00",
                coolingPower: "83.00",
                eer: "6.02",
                coolingLoadWater: "12 / 7",
                coolingSourceWater: "18 / 23",
                heating: "520.00",
                heatingPower: "122.00",
                cop: "4.26",
                heatingSourceWater: "15 / 10",
                heatingLoadWater: "40 / 45",
                pressureDrop: "38 / 45",
                cost: "46.80"
              }
            },
            {
              value: "WSHP-650",
              label: "WSHP-650",
              fields: {
                cooling: "650.00",
                coolingPower: "105.00",
                eer: "6.19",
                coolingLoadWater: "12 / 7",
                coolingSourceWater: "18 / 23",
                heating: "680.00",
                heatingPower: "156.00",
                cop: "4.36",
                heatingSourceWater: "15 / 10",
                heatingLoadWater: "40 / 45",
                pressureDrop: "44 / 51",
                cost: "59.60"
              }
            }
          ]
        },
        {
          title: "空气源热泵机组",
          selectedModel: "ASHP-450",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            {
              type: "operatingModes",
              modes: [
                {
                  title: "制冷工况",
                  rows: [
                    { label: "额定制冷量 (kW)", field: "cooling" },
                    { label: "输入功率 (kW)", field: "coolingPower" },
                    { label: "EER", field: "eer" },
                    { label: "冷水进 / 出水 (℃)", field: "coolingWater" },
                    { label: "室外干球温度 (℃)", field: "coolingOutdoor" }
                  ]
                },
                {
                  title: "制热工况",
                  rows: [
                    { label: "额定制热量 (kW)", field: "heating" },
                    { label: "输入功率 (kW)", field: "heatingPower" },
                    { label: "COP", field: "cop" },
                    { label: "热水进 / 出水 (℃)", field: "heatingWater" },
                    { label: "室外干球温度 (℃)", field: "heatingOutdoor" }
                  ]
                }
              ]
            },
            { label: "水侧压降 (kPa)", field: "pressureDrop" },
            { label: "设备参考成本 (万元)", field: "cost" }
          ],
          models: [
            {
              value: "ASHP-450",
              label: "ASHP-450",
              fields: {
                cooling: "450.00",
                coolingPower: "128.60",
                eer: "3.50",
                coolingWater: "12 / 7",
                coolingOutdoor: "35",
                heating: "490.00",
                heatingPower: "127.30",
                cop: "3.85",
                heatingWater: "40 / 45",
                heatingOutdoor: "7",
                pressureDrop: "45",
                cost: "58.60"
              }
            },
            {
              value: "ASHP-560",
              label: "ASHP-560",
              fields: {
                cooling: "560.00",
                coolingPower: "160.00",
                eer: "3.50",
                coolingWater: "12 / 7",
                coolingOutdoor: "35",
                heating: "610.00",
                heatingPower: "154.40",
                cop: "3.95",
                heatingWater: "40 / 45",
                heatingOutdoor: "7",
                pressureDrop: "49",
                cost: "71.20"
              }
            }
          ]
        },
        {
          title: "冷却塔",
          selectedModel: "CT-80",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            { label: "额定循环水量 (m³/h)", field: "flow" },
            { label: "进水温度 (℃)", value: "35" },
            { label: "出水温度 (℃)", value: "30" },
            { label: "设计湿球温度 (℃)", value: "26.8" },
            { label: "风机功率 (kW)", field: "fanPower" },
            { label: "运行噪声 dB(A)", field: "noise" },
            { label: "飘水率 (%)", field: "driftRate" }
          ],
          models: [
            {
              value: "CT-80",
              label: "CT-80",
              fields: { flow: "80", fanPower: "2.20", noise: "65", driftRate: "≤ 0.005" }
            },
            {
              value: "CT-100",
              label: "CT-100",
              fields: { flow: "100", fanPower: "3.00", noise: "67", driftRate: "≤ 0.005" }
            }
          ]
        },
        {
          title: "冷冻水泵",
          selectedModel: "ISG80-160",
          quantity: 3,
          quantityLabel: "设备数量（含备用）",
          rows: [
            { label: "水泵型号", type: "model" },
            { label: "额定流量 (m³/h)", field: "flow" },
            { label: "额定扬程 (mH₂O)", field: "head" },
            { label: "电机功率 (kW)", field: "power" },
            { label: "水泵效率 (%)", field: "efficiency" },
            { label: "额定转速 (r/min)", field: "speed" },
            { label: "控制方式", value: "变频控制" },
            { label: "运行配置", value: "2 用 1 备" }
          ],
          models: [
            {
              value: "ISG80-160",
              label: "ISG80-160",
              fields: { flow: "62.80", head: "28.00", power: "7.50", efficiency: "76", speed: "2900" }
            },
            {
              value: "ISG100-160",
              label: "ISG100-160",
              fields: { flow: "80.00", head: "28.00", power: "11.00", efficiency: "78", speed: "2900" }
            }
          ]
        },
        {
          title: "冷却水泵",
          selectedModel: "ISG100-160",
          quantity: 3,
          quantityLabel: "设备数量（含备用）",
          rows: [
            { label: "水泵型号", type: "model" },
            { label: "额定流量 (m³/h)", field: "flow" },
            { label: "额定扬程 (mH₂O)", field: "head" },
            { label: "电机功率 (kW)", field: "power" },
            { label: "水泵效率 (%)", field: "efficiency" },
            { label: "额定转速 (r/min)", field: "speed" },
            { label: "控制方式", value: "工频 / 变频" },
            { label: "运行配置", value: "2 用 1 备" }
          ],
          models: [
            {
              value: "ISG100-160",
              label: "ISG100-160",
              fields: { flow: "81.20", head: "32.00", power: "11.00", efficiency: "78", speed: "2900" }
            },
            {
              value: "ISG125-160",
              label: "ISG125-160",
              fields: { flow: "100.00", head: "32.00", power: "15.00", efficiency: "80", speed: "2900" }
            }
          ]
        },
        {
          title: "板式换热器",
          selectedModel: "PHE-600",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            { label: "额定换热量 (kW)", field: "capacity" },
            { label: "一次侧设计流量 (m³/h)", field: "primaryFlow" },
            { label: "二次侧设计流量 (m³/h)", field: "secondaryFlow" },
            { label: "一次侧进 / 出水温度 (℃)", field: "primaryTemperature" },
            { label: "二次侧进 / 出水温度 (℃)", field: "secondaryTemperature" },
            { label: "一次侧压降 (kPa)", field: "primaryDrop" },
            { label: "二次侧压降 (kPa)", field: "secondaryDrop" },
            { label: "设计压力 (MPa)", field: "pressure" },
            { label: "板片材质", field: "material" }
          ],
          models: [
            {
              value: "PHE-600",
              label: "PHE-600",
              fields: {
                capacity: "600.00",
                primaryFlow: "51.60",
                secondaryFlow: "52.80",
                primaryTemperature: "60 / 50",
                secondaryTemperature: "45 / 55",
                primaryDrop: "45",
                secondaryDrop: "42",
                pressure: "1.60",
                material: "SUS316L"
              }
            },
            {
              value: "PHE-800",
              label: "PHE-800",
              fields: {
                capacity: "800.00",
                primaryFlow: "68.80",
                secondaryFlow: "70.40",
                primaryTemperature: "60 / 50",
                secondaryTemperature: "45 / 55",
                primaryDrop: "48",
                secondaryDrop: "45",
                pressure: "1.60",
                material: "SUS316L"
              }
            }
          ]
        }
      ]
    },
    {
      title: "空气处理与末端",
      description: "空调区域设备",
      equipment: [
        {
          title: "空调机组",
          selectedModel: "AHU-12000",
          quantity: 4,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            { label: "额定风量 (m³/h)", field: "airflow" },
            { label: "额定制冷量 (kW)", field: "cooling" },
            { label: "额定制热量 (kW)", field: "heating" },
            { label: "机外余压 (Pa)", field: "pressure" },
            { label: "风机功率 (kW)", field: "fanPower" },
            { label: "新风量 (m³/h)", field: "freshAir" },
            { label: "过滤等级", field: "filter" }
          ],
          models: [
            {
              value: "AHU-12000",
              label: "AHU-12000",
              fields: { airflow: "12000", cooling: "82.00", heating: "96.00", pressure: "650", fanPower: "7.50", freshAir: "2400", filter: "G4 + F7" }
            },
            {
              value: "AHU-16000",
              label: "AHU-16000",
              fields: { airflow: "16000", cooling: "108.00", heating: "126.00", pressure: "700", fanPower: "11.00", freshAir: "3200", filter: "G4 + F7" }
            }
          ]
        },
        {
          title: "新风机组",
          selectedModel: "FAU-5000",
          quantity: 2,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            { label: "额定新风量 (m³/h)", field: "airflow" },
            { label: "额定制冷量 (kW)", field: "cooling" },
            { label: "额定制热量 (kW)", field: "heating" },
            { label: "机外余压 (Pa)", field: "pressure" },
            { label: "风机功率 (kW)", field: "fanPower" },
            { label: "热回收效率 (%)", field: "recovery" },
            { label: "过滤等级", field: "filter" }
          ],
          models: [
            {
              value: "FAU-5000",
              label: "FAU-5000",
              fields: { airflow: "5000", cooling: "52.00", heating: "61.00", pressure: "500", fanPower: "4.00", recovery: "68", filter: "G4 + F7" }
            },
            {
              value: "FAU-8000",
              label: "FAU-8000",
              fields: { airflow: "8000", cooling: "78.00", heating: "92.00", pressure: "550", fanPower: "5.50", recovery: "70", filter: "G4 + F7" }
            }
          ]
        },
        {
          title: "风机盘管",
          selectedModel: "FCU-800",
          quantity: 48,
          maxQuantity: 200,
          quantityLabel: "设备数量",
          rows: [
            { label: "设备型号", type: "model" },
            { label: "额定风量 (m³/h)", field: "airflow" },
            { label: "额定制冷量 (kW)", field: "cooling" },
            { label: "额定制热量 (kW)", field: "heating" },
            { label: "水流量 (m³/h)", field: "waterFlow" },
            { label: "水侧压降 (kPa)", field: "pressureDrop" },
            { label: "输入功率 (W)", field: "power" },
            { label: "运行噪声 dB(A)", field: "noise" }
          ],
          models: [
            {
              value: "FCU-800",
              label: "FCU-800",
              fields: { airflow: "800", cooling: "5.40", heating: "8.10", waterFlow: "0.93", pressureDrop: "30", power: "98", noise: "≤ 42" }
            },
            {
              value: "FCU-1020",
              label: "FCU-1020",
              fields: { airflow: "1020", cooling: "6.80", heating: "10.20", waterFlow: "1.17", pressureDrop: "32", power: "126", noise: "≤ 44" }
            }
          ]
        }
      ]
    }
  ];
})();
