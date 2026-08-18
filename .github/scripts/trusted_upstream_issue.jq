flatten
| any(
    .[];
    (.user.login == "github-actions[bot]")
    and (has("pull_request") | not)
    and (.title == $expected_title)
    and ((.body // "") | contains($marker))
  )
