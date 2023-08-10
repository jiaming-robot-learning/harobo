from agent.obj_nav_ura.urp.dataset import get_dataloader
import yaml

def load_config(config_path):
    with open(config_path) as f:
        config = yaml.safe_load(f)

    class Config:
        def __init__(self, config):
            self.__dict__.update(config)
    return Config(config)

data_config = load_config("configs/igp/default_data_config.yaml")

dataloader = get_dataloader(data_dir="data/info_gain",
                            split="train",
                            batch_size=10,
                            num_workers=0,
                            data_config=data_config,
                            shuffle=True)

for batch in dataloader:
    print(batch.keys())