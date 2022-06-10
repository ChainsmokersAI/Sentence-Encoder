import torch
import torch.nn as nn
import torch.distributed as dist

class SupervisedSimCSE(nn.Module):
    """
    Supervised SimCSE
    paper: SimCSE: Simple Contrastive Learning of Sentence Embeddings
    arXiv: https://arxiv.org/abs/2104.08821
    """
    def __init__(self, pretrained):
        super().__init__()
        
        # Pre-Trained LM
        self.pretrained=pretrained
        
        # Cosine Similarity
        self.cos_sim=nn.CosineSimilarity(dim=-1)
        # Temperature (Hyperparam)
        self.temp=0.05
        
        # Contrastive Loss
        self.loss=nn.CrossEntropyLoss()
        
    def pooler(self, x):
        # [CLS] without MLP (Hyperparam)
        return x.last_hidden_state[:,0,:]
    
    def get_embedding(self, x):
        # Return Sentence Representation
        x=self.pretrained(x)
        return self.pooler(x)
    
    def forward(self, sent, pos, neg):
        # Forward
        sent=self.pretrained(sent)
        pos=self.pretrained(pos)
        neg=self.pretrained(neg)
        
        # Pooling
        # Shape: batch_size x hidden_dim
        repr_sent=self.pooler(sent)
        repr_pos=self.pooler(pos)
        repr_neg=self.pooler(neg)

        # Multi-GPU
        if dist.is_initialized():
            repr_list_sent=[torch.zeros_like(repr_sent) for _ in range(dist.get_world_size())]
            repr_list_pos=[torch.zeros_like(repr_pos) for _ in range(dist.get_world_size())]
            repr_list_neg=[torch.zeros_like(repr_neg) for _ in range(dist.get_world_size())]

            # All Gather
            dist.all_gather(tensor_list=repr_list_sent, tensor=repr_sent.contiguous())
            dist.all_gather(tensor_list=repr_list_pos, tensor=repr_pos.contiguous())
            dist.all_gather(tensor_list=repr_list_neg, tensor=repr_neg.contiguous())

            # Grad Fn
            repr_list_sent[dist.get_rank()]=repr_sent
            repr_list_pos[dist.get_rank()]=repr_pos
            repr_list_neg[dist.get_rank()]=repr_neg
            
            # Shape: (world_size * batch_size) x hidden_dim
            repr_sent=torch.cat(repr_list_sent, dim=0)
            repr_pos=torch.cat(repr_list_pos, dim=0)
            repr_neg=torch.cat(repr_list_neg, dim=0)

        # Cosine Similarity
        sim_pos=self.cos_sim(repr_sent.unsqueeze(1), repr_pos.unsqueeze(0))/self.temp
        sim_neg=self.cos_sim(repr_sent.unsqueeze(1), repr_neg.unsqueeze(0))/self.temp
        
        # Contrastive Loss
        sim=torch.cat([sim_pos, sim_neg], dim=1)
        label=torch.arange(sim.size(0)).long().to(sim.device)
        loss=self.loss(sim, label)
        
        return loss

class UnsupervisedSimCSE(nn.Module):
    """
    Unsupervised SimCSE
    paper: SimCSE: Simple Contrastive Learning of Sentence Embeddings
    arXiv: https://arxiv.org/abs/2104.08821
    """
    def __init__(self, pretrained):
        super().__init__()
        
        # Pre-Trained LM
        self.pretrained=pretrained
        # Pooling Layer: MLP (Train Only)
        self.mlp=nn.Linear(self.pretrained.config.hidden_size, self.pretrained.config.hidden_size)
        
        # Cosine Similarity
        self.cos_sim=nn.CosineSimilarity(dim=-1)
        # Temperature (Hyperparam)
        self.temp=0.05
        
        # Contrastive Loss
        self.loss=nn.CrossEntropyLoss()
        
    def pooler(self, x):
        # [CLS] with MLP (Train Only)
        x=x.last_hidden_state[:,0,:]
        return self.mlp(x)
    
    def get_embedding(self, x):
        # Return Sentence Representation
        x=self.pretrained(x)
        return x.last_hidden_state[:,0,:]
    
    def forward(self, sent, pos):
        # Forward
        sent=self.pretrained(sent)
        pos=self.pretrained(pos)
        
        # Pooling
        # Shape: batch_size x hidden_dim
        repr_sent=self.pooler(sent)
        repr_pos=self.pooler(pos)

        # Multi-GPU
        if dist.is_initialized():
            repr_list_sent=[torch.zeros_like(repr_sent) for _ in range(dist.get_world_size())]
            repr_list_pos=[torch.zeros_like(repr_pos) for _ in range(dist.get_world_size())]

            # All Gather
            dist.all_gather(tensor_list=repr_list_sent, tensor=repr_sent.contiguous())
            dist.all_gather(tensor_list=repr_list_pos, tensor=repr_pos.contiguous())

            # Grad Fn
            repr_list_sent[dist.get_rank()]=repr_sent
            repr_list_pos[dist.get_rank()]=repr_pos
            
            # Shape: (world_size * batch_size) x hidden_dim
            repr_sent=torch.cat(repr_list_sent, dim=0)
            repr_pos=torch.cat(repr_list_pos, dim=0)

        # Cosine Similarity
        sim=self.cos_sim(repr_sent.unsqueeze(1), repr_pos.unsqueeze(0))/self.temp
        
        # Contrastive Loss
        label=torch.arange(sim.size(0)).long().to(sim.device)
        loss=self.loss(sim, label)
        
        return loss

class PrefixSupervisedSimCSE(nn.Module):
    """
    Supervised SimCSE with Prefix-Tuning
    paper: Prefix-Tuning: Optimizing Continuous Prompts for Generation
    arXiv: https://arxiv.org/abs/2101.00190
    paper: Deep Continuous Prompt for Contrastive Learning of Sentence Embeddings
    arXiv: https://arxiv.org/abs/2203.06875
    """
    def __init__(self, base_config, preseqlen=5, hidden_dim=512):
        super().__init__()

        ## Prefix-Tuning
        # Config of Base (Pre-Trained) LM
        self.base_config=base_config
        # Input: 0, 1, 2 ... preseqlen
        self.preseq=torch.arange(preseqlen)
        # Embedding
        self.embd=nn.Embedding(preseqlen, base_config.hidden_size)
        # Reparam
        self.reparam=nn.Sequential(
            nn.Linear(base_config.hidden_size, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, 2*base_config.num_hidden_layers*base_config.hidden_size)
        )
        # Dropout
        self.dropout=nn.Dropout(p=0.0)

        ## SimCSE
        # Cosine Similarity
        self.cos_sim=nn.CosineSimilarity(dim=-1)
        # Temperature (Hyperparam)
        self.temp=0.05
        # Contrastive Loss
        self.loss=nn.CrossEntropyLoss()
        
    def pooler(self, x):
        # [CLS] without MLP (Hyperparam)
        return x.last_hidden_state[:,0,:]

    def get_embedding(self, pretrained, x):
        # Return Sentence Representation
        prefix=self.get_prefix(batch_size=x.shape[0], device=x.device)
        x=pretrained(x, past_key_values=prefix)
        return self.pooler(x)

    def get_prefix(self, batch_size, device):
        # Return Prefix
        # batch_size, preseqlen
        preseq=self.preseq.unsqueeze(0).expand(batch_size, -1).to(device)
        # batch_size, preseqlen, hidden_size
        preseq=self.embd(preseq)
        # batch_size, preseqlen, 2*num_hidden_layers*hidden_size
        preseq=self.reparam(preseq)
        # batch_size, preseqlen, 2*num_hidden_layers, num_attention_heads, hidden_size/num_attention_heads
        preseq=preseq.reshape(
            batch_size,
            len(self.preseq),
            2*self.base_config.num_hidden_layers,
            self.base_config.num_attention_heads,
            int(self.base_config.hidden_size/self.base_config.num_attention_heads)
        )
        # 2*num_hidden_layers, batch_size, num_attention_heads, preseqlen, hidden_size/num_attention_heads
        past_key_values=preseq.permute(2, 0, 3, 1, 4)
        # Dropout
        past_key_values=self.dropout(past_key_values)

        return past_key_values.split(2)
        
    def forward(self, pretrained, sent, pos, neg):
        # Get Prefix
        prefix=self.get_prefix(batch_size=sent.shape[0], device=sent.device)
        
        # Forward with Prefix
        sent=pretrained(sent, past_key_values=prefix)
        pos=pretrained(pos, past_key_values=prefix)
        neg=pretrained(neg, past_key_values=prefix)
        
        # Pooling
        # Shape: batch_size x hidden_dim
        repr_sent=self.pooler(sent)
        repr_pos=self.pooler(pos)
        repr_neg=self.pooler(neg)

        # Multi-GPU
        if dist.is_initialized():
            repr_list_sent=[torch.zeros_like(repr_sent) for _ in range(dist.get_world_size())]
            repr_list_pos=[torch.zeros_like(repr_pos) for _ in range(dist.get_world_size())]
            repr_list_neg=[torch.zeros_like(repr_neg) for _ in range(dist.get_world_size())]

            # All Gather
            dist.all_gather(tensor_list=repr_list_sent, tensor=repr_sent.contiguous())
            dist.all_gather(tensor_list=repr_list_pos, tensor=repr_pos.contiguous())
            dist.all_gather(tensor_list=repr_list_neg, tensor=repr_neg.contiguous())

            # Grad Fn
            repr_list_sent[dist.get_rank()]=repr_sent
            repr_list_pos[dist.get_rank()]=repr_pos
            repr_list_neg[dist.get_rank()]=repr_neg
            
            # Shape: (world_size * batch_size) x hidden_dim
            repr_sent=torch.cat(repr_list_sent, dim=0)
            repr_pos=torch.cat(repr_list_pos, dim=0)
            repr_neg=torch.cat(repr_list_neg, dim=0)

        # Cosine Similarity
        sim_pos=self.cos_sim(repr_sent.unsqueeze(1), repr_pos.unsqueeze(0))/self.temp
        sim_neg=self.cos_sim(repr_sent.unsqueeze(1), repr_neg.unsqueeze(0))/self.temp
        
        # Contrastive Loss
        sim=torch.cat([sim_pos, sim_neg], dim=1)
        label=torch.arange(sim.size(0)).long().to(sim.device)
        loss=self.loss(sim, label)
        
        return loss
