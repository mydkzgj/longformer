#!/usr/bin/env python
# coding: utf-8

# <a href="https://colab.research.google.com/github/allenai/longformer/blob/master/scripts/convert_model_to_long.ipynb" target="_parent"><img src="https://colab.research.google.com/assets/colab-badge.svg" alt="Open In Colab"/></a>

# # `RoBERTa` --> `Longformer`: build a "long" version of pretrained models
# 
# This notebook replicates the procedure descriped in the [Longformer paper](https://arxiv.org/abs/2004.05150) to train a Longformer model starting from the RoBERTa checkpoint. The same procedure can be applied to build the "long" version of other pretrained models as well. 
# 

# ### Data, libraries, and imports
# Our procedure requires a corpus for pretraining. For demonstration, we will use Wikitext103; a corpus of 100M tokens from wikipedia articles. Depending on your application, consider using a different corpus that is a better match.

# In[ ]:


#get_ipython().system('wget https://s3.amazonaws.com/research.metamind.io/wikitext/wikitext-103-raw-v1.zip')
#get_ipython().system('unzip wikitext-103-raw-v1.zip')


# In[ ]:


#get_ipython().system('pip install transformers==3.0.2')


# In[2]:


import logging
import os
import math
import copy
import torch
from dataclasses import dataclass, field
from transformers import RobertaForMaskedLM, RobertaTokenizerFast, TextDataset, DataCollatorForLanguageModeling, Trainer
from transformers import TrainingArguments, HfArgumentParser
from transformers.modeling_longformer import LongformerSelfAttention

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ### RobertaLong
# 
# `RobertaLongForMaskedLM` represents the "long" version of the `RoBERTa` model. It replaces `BertSelfAttention` with `RobertaLongSelfAttention`, which is a thin wrapper around `LongformerSelfAttention`.
# 

# In[3]:


class RobertaLongSelfAttention(LongformerSelfAttention):
    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        return super().forward(hidden_states, attention_mask=attention_mask, output_attentions=output_attentions)


class RobertaLongForMaskedLM(RobertaForMaskedLM):
    def __init__(self, config):
        super().__init__(config)
        for i, layer in enumerate(self.roberta.encoder.layer):
            # replace the `modeling_bert.BertSelfAttention` object with `LongformerSelfAttention`
            layer.attention.self = RobertaLongSelfAttention(config, layer_id=i)


# Starting from the `roberta-base` checkpoint, the following function converts it into an instance of `RobertaLong`. It makes the following changes:
# 
# - extend the position embeddings from `512` positions to `max_pos`. In Longformer, we set `max_pos=4096`
# 
# - initialize the additional position embeddings by copying the embeddings of the first `512` positions. This initialization is crucial for the model performance (check table 6 in [the paper](https://arxiv.org/pdf/2004.05150.pdf) for performance without this initialization)
# 
# - replaces `modeling_bert.BertSelfAttention` objects with `modeling_longformer.LongformerSelfAttention` with a attention window size `attention_window`
# 
# The output of this function works for long documents even without pretraining. Check tables 6 and 11 in [the paper](https://arxiv.org/pdf/2004.05150.pdf) to get a sense of the expected performance of this model before pretraining.

# In[ ]:


def create_long_model(save_model_to, attention_window, max_pos):
    model = RobertaForMaskedLM.from_pretrained('roberta-base')
    tokenizer = RobertaTokenizerFast.from_pretrained('roberta-base', model_max_length=max_pos)
    config = model.config

    # extend position embeddings
    tokenizer.model_max_length = max_pos
    tokenizer.init_kwargs['model_max_length'] = max_pos
    current_max_pos, embed_size = model.roberta.embeddings.position_embeddings.weight.shape
    max_pos += 2  # NOTE: RoBERTa has positions 0,1 reserved, so embedding size is max position + 2
    config.max_position_embeddings = max_pos
    assert max_pos > current_max_pos
    # allocate a larger position embedding matrix
    new_pos_embed = model.roberta.embeddings.position_embeddings.weight.new_empty(max_pos, embed_size)
    # copy position embeddings over and over to initialize the new position embeddings
    k = 2
    step = current_max_pos - 2
    while k < max_pos - 1:
        new_pos_embed[k:(k + step)] = model.roberta.embeddings.position_embeddings.weight[2:]
        k += step
    model.roberta.embeddings.position_embeddings.weight.data = new_pos_embed
    #model.roberta.embeddings.position_ids.data = torch.tensor([i for i in range(max_pos)]).reshape(1, max_pos)

    # replace the `modeling_bert.BertSelfAttention` object with `LongformerSelfAttention`
    config.attention_window = [attention_window] * config.num_hidden_layers
    for i, layer in enumerate(model.roberta.encoder.layer):
        longformer_self_attn = LongformerSelfAttention(config, layer_id=i)
        longformer_self_attn.query = layer.attention.self.query
        longformer_self_attn.key = layer.attention.self.key
        longformer_self_attn.value = layer.attention.self.value

        longformer_self_attn.query_global = copy.deepcopy(layer.attention.self.query)
        longformer_self_attn.key_global = copy.deepcopy(layer.attention.self.key)
        longformer_self_attn.value_global = copy.deepcopy(layer.attention.self.value)

        layer.attention.self = longformer_self_attn

    logger.info(f'saving model to {save_model_to}')
    model.save_pretrained(save_model_to)
    tokenizer.save_pretrained(save_model_to)
    return model, tokenizer


# Pretraining on Masked Language Modeling (MLM) doesn't update the global projection layers. After pretraining, the following function copies `query`, `key`, `value` to their global counterpart projection matrices.
# For more explanation on "local" vs. "global" attention, please refer to the documentation [here](https://huggingface.co/transformers/model_doc/longformer.html#longformer-self-attention).

# In[ ]:


def copy_proj_layers(model):
    for i, layer in enumerate(model.roberta.encoder.layer):
        layer.attention.self.query_global = copy.deepcopy(layer.attention.self.query)
        layer.attention.self.key_global = copy.deepcopy(layer.attention.self.key)
        layer.attention.self.value_global = copy.deepcopy(layer.attention.self.value)
    return model


# ### Pretrain and Evaluate on masked language modeling (MLM)
# 
# The following function pretrains and evaluates a model on MLM.

# In[26]:


def pretrain_and_evaluate(args, model, tokenizer, eval_only, model_path):
    val_dataset = TextDataset(tokenizer=tokenizer,
                              file_path=args.val_datapath,
                              block_size=tokenizer.max_len)
    if eval_only:
        train_dataset = val_dataset
    else:
        logger.info(f'Loading and tokenizing training data is usually slow: {args.train_datapath}')
        train_dataset = TextDataset(tokenizer=tokenizer,
                                    file_path=args.train_datapath,
                                    block_size=tokenizer.max_len)

    data_collator = DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=True, mlm_probability=0.15)
    trainer = Trainer(model=model, args=args, data_collator=data_collator,
                      train_dataset=train_dataset, eval_dataset=val_dataset, prediction_loss_only=True,)

    eval_loss = trainer.evaluate()
    eval_loss = eval_loss['eval_loss']
    logger.info(f'Initial eval bpc: {eval_loss/math.log(2)}')
    
    if not eval_only:
        trainer.train(model_path=model_path)
        trainer.save_model()

        eval_loss = trainer.evaluate()
        eval_loss = eval_loss['eval_loss']
        logger.info(f'Eval bpc after pretraining: {eval_loss/math.log(2)}')


# **Training hyperparameters**
# 
# - Following RoBERTa pretraining setting, we set number of tokens per batch to be `2^18` tokens. Changing this number might require changes in the lr, lr-scheudler, #steps and #warmup steps. Therefor, it is a good idea to keep this number constant.
# 
# - Note that: `#tokens/batch = batch_size x #gpus x gradient_accumulation x seqlen`
#    
# - In [the paper](https://arxiv.org/pdf/2004.05150.pdf), we train for 65k steps, but 3k is probably enough (check table 6)
# 
# - **Important note**: The lr-scheduler in [the paper](https://arxiv.org/pdf/2004.05150.pdf) is polynomial_decay with power 3 over 65k steps. To train for 3k steps, use a constant lr-scheduler (after warmup). Both lr-scheduler are not supported in HF trainer, and at least **constant lr-scheduler** will need to be added. 
# 
# - Pretraining will take 2 days on 1 x 32GB GPU with fp32. Consider using fp16 and using more gpus to train faster (if you increase `#gpus`, reduce `gradient_accumulation` to maintain `#tokens/batch` as mentioned earlier).
# 
# - As a demonstration, this notebook is training on wikitext103 but wikitext103 is rather small that it takes 7 epochs to train for 3k steps Consider doing a single epoch on a larger dataset (800M tokens) instead.
# 
# - Set #gpus using `CUDA_VISIBLE_DEVICES`

# In[4]:


@dataclass
class ModelArgs:
    attention_window: int = field(default=512, metadata={"help": "Size of attention window"})
    max_pos: int = field(default=4096, metadata={"help": "Maximum position"})

parser = HfArgumentParser((TrainingArguments, ModelArgs,))


training_args, model_args = parser.parse_args_into_dataclasses(look_for_args_file=False, args=[
    '--output_dir', 'tmp',
    '--warmup_steps', '500',
    '--learning_rate', '0.00003',
    '--weight_decay', '0.01',
    '--adam_epsilon', '1e-6',
    '--max_steps', '3000',
    '--logging_steps', '500',
    '--save_steps', '500',
    '--max_grad_norm', '5.0',
    '--per_gpu_eval_batch_size', '8',
    '--per_gpu_train_batch_size', '2',  # 32GB gpu with fp32
    '--gradient_accumulation_steps', '32',
    '--evaluate_during_training',
    '--do_train',
    '--do_eval',
])
training_args.val_datapath = 'wikitext-103-raw/wiki.valid.raw'
training_args.train_datapath = 'wikitext-103-raw/wiki.train.raw'

# Choose GPU
import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1" #"0"


# ### Put it all together
# 
# 1) Evaluating `roberta-base` on MLM to establish a baseline. Validation `bpc` = `2.536` which is higher than the `bpc` values in table 6 [here](https://arxiv.org/pdf/2004.05150.pdf) because wikitext103 is harder than our pretraining corpus.

# In[27]:


roberta_base = RobertaForMaskedLM.from_pretrained('roberta-base')
roberta_base_tokenizer = RobertaTokenizerFast.from_pretrained('roberta-base')
logger.info('Evaluating roberta-base (seqlen: 512) for refernece ...')
pretrain_and_evaluate(training_args, roberta_base, roberta_base_tokenizer, eval_only=True, model_path=None)


# 2) As descriped in `create_long_model`, convert a `roberta-base` model into `roberta-base-4096` which is an instance of `RobertaLong`, then save it to the disk.

# In[ ]:


model_path = f'{training_args.output_dir}/roberta-base-{model_args.max_pos}'
if not os.path.exists(model_path):
    os.makedirs(model_path)

logger.info(f'Converting roberta-base into roberta-base-{model_args.max_pos}')
model, tokenizer = create_long_model(
    save_model_to=model_path, attention_window=model_args.attention_window, max_pos=model_args.max_pos)


# 3) Load `roberta-base-4096` from the disk. This model works for long sequences even without pretraining. If you don't want to pretrain, you can stop here and start finetuning your `roberta-base-4096` on downstream tasks 🎉🎉🎉

# In[ ]:


logger.info(f'Loading the model from {model_path}')
tokenizer = RobertaTokenizerFast.from_pretrained(model_path)
model = RobertaLongForMaskedLM.from_pretrained(model_path)


# 4) Pretrain `roberta-base-4096` for `3k` steps, each steps has `2^18` tokens. Notes: 
# 
# - The `training_args.max_steps = 3 ` is just for the demo. **Remove this line for the actual training**
# 
# - Training for `3k` steps will take 2 days on a single 32GB gpu with `fp32`. Consider using `fp16` and more gpus to train faster. 
# 
# - Tokenizing the training data the first time is going to take 5-10 minutes.
# 
# - MLM validation `bpc` **before** pretraining: **2.652**, a bit worse than the **2.536** of `roberta-base`. As discussed in [the paper](https://arxiv.org/pdf/2004.05150.pdf) this is expected because the model didn't learn yet to work with the sliding window attention. 
# 
# - MLM validation `bpc` after pretraining for a few number of steps: **2.628**. It is quickly getting better. By 3k steps, it should be better than the **2.536** of `roberta-base`.

# In[28]:


logger.info(f'Pretraining roberta-base-{model_args.max_pos} ... ')

training_args.max_steps = 3   ## <<<<<<<<<<<<<<<<<<<<<<<< REMOVE THIS <<<<<<<<<<<<<<<<<<<<<<<<

pretrain_and_evaluate(training_args, model, tokenizer, eval_only=False, model_path=training_args.output_dir)


# 5) Copy global projection layers. MLM pretraining doesn't train global projections, so we need to call `copy_proj_layers` to copy the local projection layers to the global ones.

# In[ ]:


logger.info(f'Copying local projection layers into global projection layers ... ')
model = copy_proj_layers(model)
logger.info(f'Saving model to {model_path}')
model.save_pretrained(model_path)


# 🎉🎉🎉🎉 **DONE**. 🎉🎉🎉🎉
# 
# `model` can now be used for finetuning on downstream tasks after loading it from the disk. 
# 
# 

# In[ ]:


logger.info(f'Loading the model from {model_path}')
tokenizer = RobertaTokenizerFast.from_pretrained(model_path)
model = RobertaLongForMaskedLM.from_pretrained(model_path)

