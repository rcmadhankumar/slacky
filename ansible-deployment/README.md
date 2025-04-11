# Slackbot deployment using ansible
### Step 1
Update inventory.ini file with your expected host information
```
# cat inventory.ini
[server]
server1.test.suse.org
```

### Step 2
Update the variables in group_vars/all.yaml
```
# cat group_vars/all.yaml
app_name: "slackbot"
image: "container image url"
container_name: "slackbot-instance"
user_name: "username for the host"
repo_re: "Regular expression for monitoring obs repositories"
slack_trigger_url: "slack webhook url"
project_re: "Regular expression for monitoring obs projects"
listen_url: "rabbit mq server ur"
host: "suse host"
openqa_host: "open qa host name"
secret_name: "secret name for storing slackbot configuration on the host"
app_config_path: "/app/slacky.ini" # constant 
```

### Step 3
Deploy the slackbot using ansible
```
# ansible-playbook -i inventory.ini roles/slackbot-app/main.yaml
```

## auto update the slacky image
run podman auto-update command on the inventory host to update the slacky image to the latest version
```
# podman auto-update
```