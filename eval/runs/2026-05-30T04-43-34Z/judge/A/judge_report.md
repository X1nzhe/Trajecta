# Judge Report

## Judge configuration

- Slot: `A`
- Model: `gemini-3.1-flash-lite`
- Prompt version: `v1_acceptability_gemini`
- Prompt SHA-256: `99ea3d934217d52f6c971b9d5547dae7c524f87d10d8493191abe41321a2cdde`

## Aggregate

- Sample count: **31**
- Acceptable: 13
- Unacceptable: 18
- **acceptable_rate: 41.9%**

## Per-case verdicts

| run_id | verdict | rationale |
| --- | --- | --- |
| `87ea181fa8c78cf62748a3490a845f4740dd4d5824cda20316ec05022998059e` | `acceptable` | The eval case correctly identifies the successful outcome and provides sufficient evidence to verify the search paramet… |
| `8dbb24c167b85f126c58dd983d130fa759dd267d562813b74a6d89ab959c1e71` | `unacceptable` | The draft classifies the outcome as a failure (missed_constraint), but the golden reference expects a success outcome.… |
| `973265788f73a2309245782ee9e5580dbcb1797dbabd1a3fc06ef8575b7db652` | `unacceptable` | The draft identifies the failure as 'missed_constraint', which is explicitly listed as a forbidden fact in the golden r… |
| `e5677cbb1e5eea79188ff5193aa5d8a3dd55b3ef5896729fdc85a8d98b6b34f3` | `unacceptable` | The proposed eval case is missing the required fields 'expected_behavior', 'actual_behavior', and 'regression_rule', wh… |
| `d95272c4bdc8b554302393460823f2cd6dfef0280f4b10b64fb3f5877cbc061a` | `acceptable` | The eval case correctly identifies the successful outcome and provides sufficient evidence to verify the agent's adhere… |
| `3672b077c54192ee2e018d1910a8c06b38e779e80e27ea7f169d586b6e52ee01` | `acceptable` | The eval case correctly identifies the successful outcome and provides evidence supporting the satisfaction of all cons… |
| `e3dd8b672040358f90c1e04eabb550ce5ae5d83f835e77b439ed6b9df83c5099` | `unacceptable` | The failure step provided (23) is outside the required range [24, 28] specified in the golden reference. Additionally,… |
| `2b5043e60689641f15f2ae8de566f023fdeece8548a5f2f2a78071c2d167f080` | `unacceptable` | The golden reference expects a 'success' outcome, but the proposed eval case treats the run as a failure. The verdict a… |
| `865eb899d535f41df5bf4b17d84eaf0ab7adea06704ad24a5fea56598831e7fa` | `unacceptable` | The proposed eval case identifies the run as a failure, but the golden reference expects a success outcome, creating a… |
| `19bacddefba25e3f6e6a63dda5c1862beeef09062e8c008f51a002aa19e2cbc3` | `acceptable` | The eval case accurately captures the failure mode, step, and reasoning, providing a clear regression rule for future t… |
| `a492a7f130f565cc31662ce63c5ed1297ff48df996a747d735405df2269a3bfb` | `unacceptable` | The draft incorrectly labels the outcome as a failure, contradicting the golden reference which expects a success outco… |
| `44bc689d47bfe634bde3454f1eab21437cc98cea7be4b6e44068e022c470ed3d` | `acceptable` | The eval case correctly identifies the successful outcome and provides sufficient evidence to support the agent's perfo… |
| `a6daae0455a6bd9e3bb37a0f9e853f53e17b7b6639cbbf5501e44c51781313d0` | `acceptable` | The eval case accurately captures the early termination failure and provides clear, evidence-backed expectations for fu… |
| `a2526a14d27f6d511e5216296976133c2d2d64126a3dfcf0cf07a94e1cd3e35d` | `unacceptable` | The proposed failure type 'missed_constraint' contradicts the golden reference's required failure type 'early_terminate… |
| `32e7dbe84bcaf8206d7d28a43e9c7b26e3553c33e509c30618bddb34fe8aaef4` | `unacceptable` | The draft incorrectly labels the outcome as a failure, whereas the golden reference expects a success outcome. Addition… |
| `963540ac95f6f5c7342cd6656329a56ee530dc17f3e6d05191643c554fac51df` | `unacceptable` | The golden reference expects a 'success' outcome, but the proposed eval case treats the run as a failure due to a misse… |
| `7cb4d4b1a9b88683ffbd82c7320a64f1a74928a0d41e9cfb2f7c52b4bc4346ab` | `unacceptable` | The draft incorrectly labels the outcome as a failure, whereas the golden reference expects a success outcome. The asse… |
| `b31ed3c90690a0e0757c108be0eafd941a32358a396f9a01d9dfeb4f1b279158` | `acceptable` | The eval case correctly identifies the failure to verify a specific hard constraint (beach resort) and provides a clear… |
| `369cde93212c31a57d18b8bc1aa61bf5d57d68bcb38d4918b4c9fc301da28a63` | `acceptable` | The eval case correctly identifies the successful outcome and provides sufficient evidence to support the conclusion th… |
| `ffa6cd8e3e896ad57c1eb59b25fe617f70d92f1b3b6b0bcaf2b734bf10d19a8b` | `unacceptable` | The draft incorrectly identifies the failure type as 'missed_constraint' instead of the golden reference's 'early_termi… |
| `945d14f4290efaa35e70185ecdb2a66c4a6acf826ae9ddb17995cc355aae0cac` | `unacceptable` | The failure type 'missed_constraint' is explicitly forbidden by the golden reference, which requires 'wrong_result'. Th… |
| `7357a951f990f531ebaa4761106d7f960054de3bb213e31a24a99f2be64264c6` | `unacceptable` | The draft violates the golden reference's forbidden facts by classifying the failure as 'wrong_target' while explicitly… |
| `2b44b6762de7b8fdfaa80b72bffbf7a9b46d957c6d5496133d96772f6c49d6e3` | `acceptable` | The eval case accurately captures the failure mode and provides clear, evidence-backed regression criteria. It correctl… |
| `0f1fafb32cb6b85f8a7e950af36286012292780d409caebd76d015af272d099c` | `unacceptable` | The draft identifies the run as a failure, but the golden reference expects a success outcome, creating a fundamental m… |
| `f51766eee5627636023c32517eb5d5362c75137194490409e8935e1035e77e2a` | `unacceptable` | The draft's failure type and forbidden facts conflict with the golden reference's requirements. The golden reference ex… |
| `cc5c498fce92fc22a65d6efc8c7f6deae6b032c3fdc0d135346fa5e476a89923` | `acceptable` | The eval case accurately captures the failure to meet a specific price constraint and provides a clear regression rule… |
| `2d0807be8dc5f2d1219d6c2b7b2f17d00a12178f0f25a1e0f0c0d11e12f1b95b` | `acceptable` | The eval case correctly identifies the successful outcome and provides sufficient evidence to verify the search results… |
| `98872d07da3c778a6c97369b1ac1b7e77e37e6829554add5841dd587705ad59e` | `unacceptable` | The proposed eval case identifies the outcome as a failure, which directly contradicts the golden reference's requireme… |
| `071b66b4835f126f6c205fa349323688f6652d66b103c66cf1606f5abe36ba28` | `acceptable` | The eval case accurately captures the failure to address the 'recently created' constraint and provides a clear regress… |
| `cf1b44c865dd9dd3d6a5742720640bb2907757b914b616356cb7d0b10c2582da` | `acceptable` | The eval case accurately captures the failure to apply a specific constraint and correctly identifies the inefficient s… |
| `d8d335be0b252e0adbef1c41f190ee7b249d37ac857224bf8857473dc227c258` | `unacceptable` | The proposed failure_type 'early_terminated' is explicitly forbidden by the golden reference, which requires 'inefficie… |
