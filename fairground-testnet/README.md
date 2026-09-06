# Fairground Testnet

Пакет Soft Hub для сезона 3 Fairground. Ставится как `.softhub.zip`, ключи и прокси живут в Vault хаба.

## Действия

- **Работа** — Connect Rabby в Ads (пароль email в Hub = пароль кошелька), сделки ключом из Hub.
- **Закрыть всё** — снять ордера и позиции.
- **Кран** — Arbitrum Sepolia ETH через AdsPower / QuickNode.
- **Парсинг** — ETH, USDC, объём, позиции.

Сборка:

```bash
python3 scripts/build_plugin.py fairground-testnet dist/fairground-testnet-1.6.0.softhub.zip
```
