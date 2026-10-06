# 业务洞察 (Insights)

> 本文件由 `src/churn_model.py` 自动生成/更新。运行完整流水线
> （`python scripts/run_pipeline.py`）后，这里会被替换为**本次数据**的真实结论，
> 包括：三模型对比、SHAP 关键特征、频次区间 × 流失率、未来 30 天 ARIMA 预测结论。

## 运行后你会看到的示例结构

- **三种离散分类器对比**：LightGBM / XGBoost / RandomForest 的 AUC / F1 / Precision / Recall
- **驱动流失的关键特征 (SHAP)**
- **单样本解释**：当前最高风险地址为何被判为高流失
- **交易频次区间 × 流失率**：定位"危险频次区间"
- **未来 30 天 ARIMA 预测**：人均频次下降曲线 + 逐日流失率 + 结论
