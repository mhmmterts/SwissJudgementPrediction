from dataclasses import dataclass
from typing import Optional, Tuple
import numpy as np

import torch
from torch import nn
from transformers.file_utils import ModelOutput


@dataclass
class SimpleOutput(ModelOutput):
    last_hidden_state: torch.FloatTensor = None
    past_key_values: Optional[Tuple[Tuple[torch.FloatTensor]]] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None
    attentions: Optional[Tuple[torch.FloatTensor]] = None
    cross_attentions: Optional[Tuple[torch.FloatTensor]] = None


def sinusoidal_init(num_embeddings: int, embedding_dim: int):
    # keep dim 0 for padding token position encoding zero vector
    position_enc = np.array([
        [pos / np.power(10000, 2 * i / embedding_dim) for i in range(embedding_dim)]
        if pos != 0 else np.zeros(embedding_dim) for pos in range(num_embeddings)])

    position_enc[1:, 0::2] = np.sin(position_enc[1:, 0::2])  # dim 2i
    position_enc[1:, 1::2] = np.cos(position_enc[1:, 1::2])  # dim 2i+1
    return torch.from_numpy(position_enc).type(torch.FloatTensor)


# TODO subclass BertModel, BertConfig and BertTokenizer to make it more clean and to override save_pretrained() so that the seg_encoder is saved too
class HierarchicalBert(nn.Module):

    def __init__(self, encoder, max_segments, max_segment_length, seg_encoder_type="transformer"):
        super(HierarchicalBert, self).__init__()
        supported_models = ['bert', 'camembert', 'xlm-roberta', 'roberta']
        assert encoder.config.model_type in supported_models  # other models are not supported so far

        # Pre-trained segment (token-wise) encoder, e.g., BERT
        self.encoder = encoder

        # Specs for the segment-wise encoder
        self.hidden_size = encoder.config.hidden_size
        self.max_segments = max_segments
        self.max_segment_length = max_segment_length
        self.seg_encoder_type = seg_encoder_type

        if self.seg_encoder_type == "lstm":
            # Init segment-wise BiLSTM-based encoder
            self.seg_encoder = nn.LSTM(encoder.config.hidden_size, encoder.config.hidden_size,
                                       bidirectional=True, num_layers=1, batch_first=True)
            self.down_project = nn.Linear(in_features=2 * self.hidden_size, out_features=self.hidden_size)
        if self.seg_encoder_type == "transformer":
            # Init sinusoidal positional embeddings
            weight = sinusoidal_init(max_segments + 1, encoder.config.hidden_size)
            self.seg_pos_embeddings = nn.Embedding(max_segments + 1, encoder.config.hidden_size,
                                                   padding_idx=0, _weight=weight)

            # Init segment-wise transformer-based encoder
            self.seg_encoder = nn.Transformer(d_model=encoder.config.hidden_size,
                                              nhead=encoder.config.num_attention_heads,
                                              batch_first=True, dim_feedforward=encoder.config.intermediate_size,
                                              activation=encoder.config.hidden_act,
                                              dropout=encoder.config.hidden_dropout_prob,
                                              layer_norm_eps=encoder.config.layer_norm_eps,
                                              num_encoder_layers=2, num_decoder_layers=0).encoder

    def forward(self,
                input_ids=None,
                attention_mask=None,
                token_type_ids=None,
                position_ids=None,
                head_mask=None,
                inputs_embeds=None,
                labels=None,
                output_attentions=None,
                output_hidden_states=None,
                return_dict=None,
                adapter_names=None,
                ):
        # Input (samples, segments, max_segment_length) --> (16, 10, 510)
        # Squash samples and segments into a single axis (samples * segments, max_segment_length) --> (160, 512)
        input_ids_reshape = input_ids.contiguous().view(-1, input_ids.size(-1))
        attention_mask_reshape = attention_mask.contiguous().view(-1, attention_mask.size(-1))
        token_type_ids_reshape = token_type_ids.contiguous().view(-1, token_type_ids.size(-1))

        # Encode segments with BERT --> (160, 512, 768)
        encoder_outputs = self.encoder(input_ids=input_ids_reshape,
                                       attention_mask=attention_mask_reshape,
                                       token_type_ids=token_type_ids_reshape)[0]

        # Reshape back to (samples, segments, max_segment_length, output_size) --> (16, 10, 512, 768)
        encoder_outputs = encoder_outputs.contiguous().view(input_ids.size(0), self.max_segments,
                                                            self.max_segment_length, self.hidden_size)

        # Gather CLS per segment --> (16, 10, 768)
        encoder_outputs = encoder_outputs[:, :, 0]

        if self.seg_encoder_type == 'lstm':
            # LSTMs on top of segment encodings --> (16, 10, 1536)
            lstms = self.seg_encoder(encoder_outputs)

            # Reshape LSTM outputs to split directions -->  (16, 10, 2, 768)
            reshaped_lstms = lstms[0].view(input_ids.size(0), self.max_segments, 2, self.hidden_size)

            # Concatenate of first and last hidden states -->  (16, 1536)
            seg_encoder_outputs = torch.cat((reshaped_lstms[:, -1, 0, :], reshaped_lstms[:, 0, 1, :]), -1)

            # Down-project -->  (16, 768)
            outputs = self.down_project(seg_encoder_outputs)

        if self.seg_encoder_type == 'transformer':
            # Transformer on top of segment encodings --> (16, 10, 768)
            # Infer real segments, i.e., mask paddings (like attention_mask but on a segment level)
            seg_mask = (torch.sum(input_ids, 2) != 0).to(input_ids.dtype)
            # Infer and collect segment positional embeddings
            seg_positions = torch.arange(1, self.max_segments + 1).to(input_ids.device) * seg_mask
            # Add segment positional embeddings to segment inputs
            encoder_outputs += self.seg_pos_embeddings(seg_positions)

            # Encode segments with segment-wise transformer
            seg_encoder_outputs = self.seg_encoder(encoder_outputs)

            # Collect document representation
            outputs, _ = torch.max(seg_encoder_outputs, 1)

        return SimpleOutput(last_hidden_state=outputs, hidden_states=outputs)
