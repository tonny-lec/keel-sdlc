# 設計の参考資料

KEEL固有の状態機械、スキーマ、証拠の失効規則、優先順位の式は本実装の設計判断である。以下の文書への準拠認証や、すべての推奨事項の実装を意味しない。

## 開発工程へ組み込むセキュリティ

[NIST SP 800-218: SSDF v1.1](https://csrc.nist.gov/pubs/sp/800/218/final)は、各種SDLCに組み込める高水準のセキュア開発プラクティスを整理している。KEELでは、要求と検証の明示、成果物と証拠の保管、リリース後の学習を工程に含める設計の参考にした。参照した版は2022年2月のv1.1であり、最新の全NIST文書を網羅するという主張はしない。

## 小さく配備して観測する

[Google SRE Workbook: Canarying Releases](https://sre.google/workbook/canarying-releases/)は、変更を限定した範囲と時間で評価して拡大判断につなげる考え方を説明している。KEELでは、配置前の成果物識別、停止条件、回復計画、観測期間、配置後のチェックを表現する。本実装自体にカナリア配備器やメトリクス収集器は含めない。

## CI部品

- [actions/checkout](https://github.com/actions/checkout)：v7タグが指すコミットを確認し、CIではSHAで固定した。
- [actions/setup-python](https://github.com/actions/setup-python)：v6タグが指すコミットを確認し、CIではSHAで固定した。

参照とタグ確認は2026-09-07に実施。外部部品の更新は別途レビューして反映する。
