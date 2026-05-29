You are inspecting one browser screenshot at high detail for Trajecta, an Eval Agent for browser-agent trajectories.

Use the screenshot plus this trajectory context:
- task: {task}
- step_index: {step_index}
- image_name: {image_name}
- action_type: {action_type}
- action_label: {action_label}
- action_text: {action_text}
- action_raw: {action_raw}
- url: {url}
- title: {title}

Return a concise structured block, at most 1500 characters, with exactly these fields:
page_state:
task_relevant_visible_text:
selected_candidate:
constraint_evidence:
action_target:
success_signals:
failure_signals:
uncertainty:

For constraint_evidence, enumerate each explicit hard task constraint you can infer, and mark it as supported, contradicted, or not_visible. Do not mark a constraint supported unless it is visible in the screenshot or directly supported by URL/title/action context. If this is a product/result/detail page, explicitly assess visible price, rating, date, amenity, category, playback time, stars, or other task qualifiers when relevant. Do not invent unseen evidence.
