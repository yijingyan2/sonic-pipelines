#!/bin/bash

set -ex
gh --version || { curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && apt update && apt install gh -y; }

. .bashenv
echo $GH_TOKEN | gh auth login --with-token
git config --global user.email "sonicbld@microsoft.com"
git config --global user.name "Sonic Build Admin"
work_dir=$(pwd)

# $1 is a single label.
check_conflict(){
    branch_label=$(echo $1 | grep -Eo "(msft-)?[0-9]{6}")
    cd $work_dir
    rm -rf $REPO-$branch_label
    mkdir $REPO-$branch_label
    cd $REPO-$branch_label
    git init
    git remote add head https://github.com/$ORG/$REPO
    if echo $branch_label | grep msft-; then
        if [[ $REPO == "sonic-buildimage" ]]; then
            git remote add base https://github.com/Azure/$REPO-msft
            git remote add mssonicbld https://mssonicbld:$GH_TOKEN@github.com/mssonicbld/$REPO-msft
            newpr_base="Azure/$REPO-msft"
        else
            git remote add base https://github.com/Azure/$REPO.msft
            git remote add mssonicbld https://mssonicbld:$GH_TOKEN@github.com/mssonicbld/$REPO.msft
            newpr_base="Azure/$REPO.msft"
        fi
    else
        git remote add base https://github.com/$ORG/$REPO
        git remote add mssonicbld https://mssonicbld:$GH_TOKEN@github.com/mssonicbld/$REPO
        newpr_base="$ORG/$REPO"
    fi
    git remote update
    git status
    git checkout -b $PR_BASE_BRANCH --track head/$PR_BASE_BRANCH
    git status
    contains_submodule=""
    if [[ "$PR_MERGED" == "true" ]];then
        git reset $PR_COMMIT_SHA --hard
        contains_submodule=$(git show HEAD | grep -Eo "^\+Subproject commit ")
        git reset HEAD~
        git add . -f
    else
        git fetch head +refs/pull/$PR_NUMBER/merge:refs/remotes/pull/$PR_NUMBER/merge
        contains_submodule=$(git log head/$PR_BASE_BRANCH..$PR_COMMIT_SHA -p | grep -Eo "^\+Subproject commit ")
        git merge pull/$PR_NUMBER/merge --squash || { echo "PR is Out of Date!"; return 253; }
    fi
    if [ -n "$contains_submodule" ]; then
        echo "PR contains submodule change"
        gh pr comment $PR_URL --body "Auto cherry pick don't support submodule update. Please manually cherry pick!"
        return 251
    fi
    content=$(gh pr view $PR_URL --json title,body)
    title=$(echo "$content" | jq .title -r)
    body=$(echo "$content" | jq .body -r)
    git status
    git commit --signoff -m "$title" -m "$body"
    git status
    commit=$(git log -n 1 --format=%H)
    target_branch=$(echo $branch_label | grep -Eo [0-9]*)
    git checkout -b $target_branch --track base/$target_branch || { echo "$target_branch didn't exist!"; return 252; }
    git status
    rc=''
    if git cherry-pick $commit; then
        gh pr edit $PR_URL --remove-label "Cherry Pick Conflict_$branch_label" || true
        sleep 1
        return 0
    else
        gh pr edit $PR_URL --add-label "Cherry Pick Conflict_$branch_label" || true
        sleep 1
        return 254
    fi
}

create_pr(){
    check_conflict "$1"
    [[ "$PR_MERGED" != "true" ]] && echo "PR not merged!" && return 0
    git status
    git push mssonicbld HEAD:cherry/$branch_label/$PR_NUMBER -f
    title="[action] [PR:$PR_NUMBER] $(git log -n 1 --pretty=format:'%s')"
    git log -n 1 --pretty=format:'%b' > body
    result=$(gh pr create -R $newpr_base -H mssonicbld:cherry/$branch_label/$PR_NUMBER -B $target_branch -t "$title" -F body -l "automerge" 2>&1 || true)
    sleep 1
    echo $result | grep "already exists" && return 0 || true
    new_pr_url=$(echo $result | grep -Eo https://github.com.*)
    gh pr comment $new_pr_url --body "Original PR: $PR_URL"
    echo $new_pr_url | grep 'github.com/Azure' && sleep 1 && gh pr comment $new_pr_url --body "/azp run"
    sleep 1
    gh pr edit $PR_URL --add-label "Created PR to $branch_label Branch"
    sleep 1
    gh pr comment $PR_URL --body "Cherry-pick PR to $branch_label: ${new_pr_url}"
    sleep 1
}

labeled(){
    echo [ AUTO CHERRY PICK ] labeled: $ACTION_LABEL $PR_URL
    if [[ $REPO == "sonic-mgmt" ]]; then
        echo $ACTION_LABEL | grep msft- || return 0
    fi
    if echo $ACTION_LABEL | grep -E '^Approved for (msft-)?[0-9]{6} Branch$'; then
        create_pr "$ACTION_LABEL"
        return $?
    fi
    if echo $ACTION_LABEL | grep -E '^Request for (msft-)?[0-9]{6} Branch$'; then
        check_conflict "$ACTION_LABEL"
    fi

}

synchronize(){
    echo [ AUTO CHERRY PICK ] synchronize: $PR_LABELS $PR_URL
    IFS=, read -a labels <<< $PR_LABELS
    for label in "${labels[@]}"; do
        if echo $label | grep -E '^Request for (msft-)?[0-9]{6} Branch$'; then
            if [[ $REPO == "sonic-mgmt" ]];then
                echo $label | grep msft- || continue
            fi
            check_conflict "$label" || true
        fi
    done
}

closed(){
    echo [ AUTO CHERRY PICK ] closed: $PR_LABELS $PR_URL
    IFS=, read -a labels <<< $PR_LABELS
    for label in "${labels[@]}"; do
        if echo $label | grep -E '^Approved for (msft-)?[0-9]{6} Branch$'; then
            if [[ $REPO == "sonic-mgmt" ]];then
                echo $label | grep msft- || continue
            fi
            create_pr "$label" || true
        fi
    done
}

$ACTION
