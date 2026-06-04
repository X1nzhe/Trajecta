# Judge Agreement Report (κ_LLM,LLM)

## Judges

- **Judge A (slot A)**: model `gemini-3.1-flash-lite`, prompt `v1_acceptability_gemini` (sha256 `99ea3d934217d52f6c971b9d5547dae7c524f87d10d8493191abe41321a2cdde`)
- **Judge B (slot B)**: model `gpt-5.4-mini-2026-03-17`, prompt `v1_acceptability_openai` (sha256 `95c129141577828391eff8a516099b7a2964a3af900719909d687f2fb92be021`)

## Sample

- Sample size: **31**
- Selection policy: `full_31_preferred`

## Acceptable rates

| Judge | acceptable / total | acceptable_rate |
| --- | --- | --- |
| A | 13 / 31 | 41.9% |
| B | 15 / 31 | 48.4% |

## Agreement

- **κ_LLM,LLM**: `0.7406` (target ≥ 0.6) — PASS — disagreement analysis not required
- Agreement: 27 / 31 (87.1%)
- Disagreements: 4

## Per-case verdicts

| run_id | Judge A | Judge B | agree |
| --- | --- | --- | --- |
| `87ea181fa8c78cf62748a3490a845f4740dd4d5824cda20316ec05022998059e` | `acceptable` | `acceptable` | ✓ |
| `8dbb24c167b85f126c58dd983d130fa759dd267d562813b74a6d89ab959c1e71` | `unacceptable` | `unacceptable` | ✓ |
| `973265788f73a2309245782ee9e5580dbcb1797dbabd1a3fc06ef8575b7db652` | `unacceptable` | `unacceptable` | ✓ |
| `e5677cbb1e5eea79188ff5193aa5d8a3dd55b3ef5896729fdc85a8d98b6b34f3` | `unacceptable` | `acceptable` | ✗ |
| `d95272c4bdc8b554302393460823f2cd6dfef0280f4b10b64fb3f5877cbc061a` | `acceptable` | `acceptable` | ✓ |
| `3672b077c54192ee2e018d1910a8c06b38e779e80e27ea7f169d586b6e52ee01` | `acceptable` | `acceptable` | ✓ |
| `e3dd8b672040358f90c1e04eabb550ce5ae5d83f835e77b439ed6b9df83c5099` | `unacceptable` | `acceptable` | ✗ |
| `2b5043e60689641f15f2ae8de566f023fdeece8548a5f2f2a78071c2d167f080` | `unacceptable` | `unacceptable` | ✓ |
| `865eb899d535f41df5bf4b17d84eaf0ab7adea06704ad24a5fea56598831e7fa` | `unacceptable` | `unacceptable` | ✓ |
| `19bacddefba25e3f6e6a63dda5c1862beeef09062e8c008f51a002aa19e2cbc3` | `acceptable` | `acceptable` | ✓ |
| `a492a7f130f565cc31662ce63c5ed1297ff48df996a747d735405df2269a3bfb` | `unacceptable` | `unacceptable` | ✓ |
| `44bc689d47bfe634bde3454f1eab21437cc98cea7be4b6e44068e022c470ed3d` | `acceptable` | `acceptable` | ✓ |
| `a6daae0455a6bd9e3bb37a0f9e853f53e17b7b6639cbbf5501e44c51781313d0` | `acceptable` | `acceptable` | ✓ |
| `a2526a14d27f6d511e5216296976133c2d2d64126a3dfcf0cf07a94e1cd3e35d` | `unacceptable` | `unacceptable` | ✓ |
| `32e7dbe84bcaf8206d7d28a43e9c7b26e3553c33e509c30618bddb34fe8aaef4` | `unacceptable` | `unacceptable` | ✓ |
| `963540ac95f6f5c7342cd6656329a56ee530dc17f3e6d05191643c554fac51df` | `unacceptable` | `acceptable` | ✗ |
| `7cb4d4b1a9b88683ffbd82c7320a64f1a74928a0d41e9cfb2f7c52b4bc4346ab` | `unacceptable` | `unacceptable` | ✓ |
| `b31ed3c90690a0e0757c108be0eafd941a32358a396f9a01d9dfeb4f1b279158` | `acceptable` | `unacceptable` | ✗ |
| `369cde93212c31a57d18b8bc1aa61bf5d57d68bcb38d4918b4c9fc301da28a63` | `acceptable` | `acceptable` | ✓ |
| `ffa6cd8e3e896ad57c1eb59b25fe617f70d92f1b3b6b0bcaf2b734bf10d19a8b` | `unacceptable` | `unacceptable` | ✓ |
| `945d14f4290efaa35e70185ecdb2a66c4a6acf826ae9ddb17995cc355aae0cac` | `unacceptable` | `unacceptable` | ✓ |
| `7357a951f990f531ebaa4761106d7f960054de3bb213e31a24a99f2be64264c6` | `unacceptable` | `unacceptable` | ✓ |
| `2b44b6762de7b8fdfaa80b72bffbf7a9b46d957c6d5496133d96772f6c49d6e3` | `acceptable` | `acceptable` | ✓ |
| `0f1fafb32cb6b85f8a7e950af36286012292780d409caebd76d015af272d099c` | `unacceptable` | `unacceptable` | ✓ |
| `f51766eee5627636023c32517eb5d5362c75137194490409e8935e1035e77e2a` | `unacceptable` | `unacceptable` | ✓ |
| `cc5c498fce92fc22a65d6efc8c7f6deae6b032c3fdc0d135346fa5e476a89923` | `acceptable` | `acceptable` | ✓ |
| `2d0807be8dc5f2d1219d6c2b7b2f17d00a12178f0f25a1e0f0c0d11e12f1b95b` | `acceptable` | `acceptable` | ✓ |
| `98872d07da3c778a6c97369b1ac1b7e77e37e6829554add5841dd587705ad59e` | `unacceptable` | `unacceptable` | ✓ |
| `071b66b4835f126f6c205fa349323688f6652d66b103c66cf1606f5abe36ba28` | `acceptable` | `acceptable` | ✓ |
| `cf1b44c865dd9dd3d6a5742720640bb2907757b914b616356cb7d0b10c2582da` | `acceptable` | `acceptable` | ✓ |
| `d8d335be0b252e0adbef1c41f190ee7b249d37ac857224bf8857473dc227c258` | `unacceptable` | `unacceptable` | ✓ |

