#####################################################
# Copyright (c) Xuanyi Dong [GitHub D-X-Y], 2021.04 #
#####################################################
import torch

import torch.nn.functional as F

from xautodl.xlayers import super_core
from xautodl.xlayers import trunc_normal_
from xautodl.models.xcore import get_model


class MetaModelV1(super_core.SuperModule):
    """Learning to Generate Models One Step Ahead (Meta Model Design)."""

    def __init__(
        self,
        shape_container,
        layer_dim,
        time_dim,
        meta_timestamps,
        mha_depth: int = 2,
        dropout: float = 0.1,
        seq_length: int = 10,
        interval: float = None,
        thresh: float = None,
    ):
        super(MetaModelV1, self).__init__()
        self._shape_container = shape_container
        self._num_layers = len(shape_container)
        self._numel_per_layer = []
        for ilayer in range(self._num_layers):
            self._numel_per_layer.append(shape_container[ilayer].numel())
        self._raw_meta_timestamps = meta_timestamps
        assert interval is not None
        self._interval = interval
        self._seq_length = seq_length
        self._thresh = interval * 30 if thresh is None else thresh

        self.register_parameter(
            "_super_layer_embed",
            torch.nn.Parameter(torch.Tensor(self._num_layers, layer_dim)),
        )
        self.register_parameter(
            "_super_meta_embed",
            torch.nn.Parameter(torch.Tensor(len(meta_timestamps), time_dim)),
        )
        self.register_buffer("_meta_timestamps", torch.Tensor(meta_timestamps))
        # register a time difference buffer
        time_interval = [-i * self._interval for i in range(self._seq_length)]
        time_interval.reverse()
        self.register_buffer("_time_interval", torch.Tensor(time_interval))
        self._time_embed_dim = time_dim
        self._append_meta_embed = dict(fixed=None, learnt=None)
        self._append_meta_timestamps = dict(fixed=None, learnt=None)

        self._tscalar_embed = super_core.SuperDynamicPositionE(
            time_dim, scale=1 / interval
        )

        # build transformer
        self._trans_att = super_core.SuperQKVAttentionV2(
            qk_att_dim=time_dim,
            in_v_dim=time_dim,
            hidden_dim=time_dim,
            num_heads=4,
            proj_dim=time_dim,
            qkv_bias=True,
            attn_drop=None,
            proj_drop=dropout,
        )
        layers = []
        for ilayer in range(mha_depth):
            layers.append(
                super_core.SuperTransformerEncoderLayer(
                    time_dim * 2,
                    4,
                    True,
                    4,
                    dropout,
                    norm_affine=False,
                    order=super_core.LayerOrder.PostNorm,
                    use_mask=True,
                )
            )
        layers.append(super_core.SuperLinear(time_dim * 2, time_dim))
        self._meta_corrector = super_core.SuperSequential(*layers)

        model_kwargs = dict(
            config=dict(model_type="dual_norm_mlp"),
            input_dim=layer_dim + time_dim,
            output_dim=max(self._numel_per_layer),
            hidden_dims=[(layer_dim + time_dim) * 2] * 3,
            act_cls="gelu",
            norm_cls="layer_norm_1d",
            dropout=dropout,
        )
        self._generator = get_model(**model_kwargs)

        # initialization
        trunc_normal_(
            [self._super_layer_embed, self._super_meta_embed],
            std=0.02,
        )

    def get_parameters(self, time_embed, meta_corrector, generator):
        parameters = []
        if time_embed:
            parameters.append(self._super_meta_embed)
        if meta_corrector:
            parameters.extend(list(self._trans_att.parameters()))
            parameters.extend(list(self._meta_corrector.parameters()))
        if generator:
            parameters.append(self._super_layer_embed)
            parameters.extend(list(self._generator.parameters()))
        return parameters

    @property
    def meta_timestamps(self):
        with torch.no_grad():
            meta_timestamps = [self._meta_timestamps]
            for key in ("fixed", "learnt"):
                if self._append_meta_timestamps[key] is not None:
                    meta_timestamps.append(self._append_meta_timestamps[key])
        return torch.cat(meta_timestamps)

    @property
    def super_meta_embed(self):
        meta_embed = [self._super_meta_embed]
        for key in ("fixed", "learnt"):
            if self._append_meta_embed[key] is not None:
                meta_embed.append(self._append_meta_embed[key])
        return torch.cat(meta_embed)

    def create_meta_embed(self):
        param = torch.Tensor(1, self._time_embed_dim)
        trunc_normal_(param, std=0.02)
        param = param.to(self._super_meta_embed.device)
        param = torch.nn.Parameter(param, True)
        return param

    def get_closest_meta_distance(self, timestamp):
        with torch.no_grad():
            distances = torch.abs(self.meta_timestamps - timestamp)
            return torch.min(distances).item()

    def replace_append_learnt(self, timestamp, meta_embed):
        self._append_meta_timestamps["learnt"] = timestamp
        self._append_meta_embed["learnt"] = meta_embed

    @property
    def meta_length(self):
        return self.meta_timestamps.numel()

    def clear_fixed(self):
        self._append_meta_timestamps["fixed"] = None
        self._append_meta_embed["fixed"] = None

    def clear_learnt(self):
        self.replace_append_learnt(None, None)

    def append_fixed(self, timestamp, meta_embed):
        with torch.no_grad():
            device = self._super_meta_embed.device
            timestamp = timestamp.detach().clone().to(device)
            meta_embed = meta_embed.detach().clone().to(device)
            if self._append_meta_timestamps["fixed"] is None:
                self._append_meta_timestamps["fixed"] = timestamp
            else:
                self._append_meta_timestamps["fixed"] = torch.cat(
                    (self._append_meta_timestamps["fixed"], timestamp), dim=0
                )
            if self._append_meta_embed["fixed"] is None:
                self._append_meta_embed["fixed"] = meta_embed
            else:
                self._append_meta_embed["fixed"] = torch.cat(
                    (self._append_meta_embed["fixed"], meta_embed), dim=0
                )

    def _obtain_time_embed(self, timestamps):
        # timestamps is a batch of sequence of timestamps
        batch, seq = timestamps.shape
        meta_timestamps, meta_embeds = self.meta_timestamps, self.super_meta_embed
        timestamp_v_embed = meta_embeds.unsqueeze(dim=0)
        timestamp_qk_att_embed = self._tscalar_embed(
            torch.unsqueeze(timestamps, dim=-1) - meta_timestamps
        )
        # create the mask
        mask = (
            torch.unsqueeze(timestamps, dim=-1) <= meta_timestamps.view(1, 1, -1)
        ) | (
            torch.abs(
                torch.unsqueeze(timestamps, dim=-1) - meta_timestamps.view(1, 1, -1)
            )
            > self._thresh
        )
        timestamp_embeds = self._trans_att(
            timestamp_qk_att_embed,
            timestamp_v_embed,
            mask,
        )
        relative_timestamps = timestamps - timestamps[:, :1]
        relative_pos_embeds = self._tscalar_embed(relative_timestamps)
        init_timestamp_embeds = torch.cat(
            (timestamp_embeds, relative_pos_embeds), dim=-1
        )
        corrected_embeds = self._meta_corrector(init_timestamp_embeds)
        return corrected_embeds

    def forward_raw(self, timestamps, time_embeds, get_seq_last):
        if time_embeds is None:
            time_seq = timestamps.view(-1, 1) + self._time_interval.view(1, -1)
            B, S = time_seq.shape
            time_embeds = self._obtain_time_embed(time_seq)
        else:
            time_seq = None
            B, S, _ = time_embeds.shape
        # create joint embed
        num_layer, _ = self._super_layer_embed.shape
        if get_seq_last:
            time_embeds = time_embeds[:, -1, :]
            # The shape of `joint_embed` is batch * num-layers * input-dim
            joint_embeds = torch.cat(
                (
                    time_embeds.view(B, 1, -1).expand(-1, num_layer, -1),
                    self._super_layer_embed.view(1, num_layer, -1).expand(B, -1, -1),
                ),
                dim=-1,
            )
        else:
            # The shape of `joint_embed` is batch * seq * num-layers * input-dim
            joint_embeds = torch.cat(
                (
                    time_embeds.view(B, S, 1, -1).expand(-1, -1, num_layer, -1),
                    self._super_layer_embed.view(1, 1, num_layer, -1).expand(
                        B, S, -1, -1
                    ),
                ),
                dim=-1,
            )
        batch_weights = self._generator(joint_embeds)
        batch_containers = []
        for weights in torch.split(batch_weights, 1):
            if get_seq_last:
                batch_containers.append(
                    self._shape_container.translate(torch.split(weights.squeeze(0), 1))
                )
            else:
                seq_containers = []
                for ws in torch.split(weights.squeeze(0), 1):
                    seq_containers.append(
                        self._shape_container.translate(torch.split(ws.squeeze(0), 1))
                    )
                batch_containers.append(seq_containers)
        return time_seq, batch_containers, time_embeds

    def forward_candidate(self, input):
        raise NotImplementedError

    def adapt(self, base_model, criterion, timestamp, x, y, lr, epochs, init_info):
        distance = self.get_closest_meta_distance(timestamp)
        if distance + self._interval * 1e-2 <= self._interval:
            return False, None
        x, y = x.to(self._meta_timestamps.device), y.to(self._meta_timestamps.device)
        with torch.set_grad_enabled(True):
            new_param = self.create_meta_embed()
            optimizer = torch.optim.Adam(
                [new_param], lr=lr, weight_decay=1e-5, amsgrad=True
            )
            timestamp = torch.Tensor([timestamp]).to(new_param.device)
            self.replace_append_learnt(timestamp, new_param)
            self.train()
            base_model.train()
            if init_info is not None:
                best_loss = init_info["loss"]
                new_param.data.copy_(init_info["param"].data)
            else:
                best_loss = 1e9
            with torch.no_grad():
                best_new_param = new_param.detach().clone()
            for iepoch in range(epochs):
                optimizer.zero_grad()
                _, [_], time_embed = self(timestamp.view(1, 1), None, True)
                match_loss = criterion(new_param, time_embed)

                _, [container], time_embed = self(None, new_param.view(1, 1, -1), True)
                y_hat = base_model.forward_with_container(x, container)
                meta_loss = criterion(y_hat, y)
                loss = meta_loss + match_loss
                loss.backward()
                optimizer.step()
                # print("{:03d}/{:03d} : loss : {:.4f} = {:.4f} + {:.4f}".format(iepoch, epochs, loss.item(), meta_loss.item(), match_loss.item()))
                if meta_loss.item() < best_loss:
                    with torch.no_grad():
                        best_loss = meta_loss.item()
                        best_new_param = new_param.detach().clone()
        with torch.no_grad():
            self.replace_append_learnt(None, None)
            self.append_fixed(timestamp, best_new_param)
        return True, meta_loss.item()

    def extra_repr(self) -> str:
        return "(_super_layer_embed): {:}, (_super_meta_embed): {:}, (_meta_timestamps): {:}".format(
            list(self._super_layer_embed.shape),
            list(self._super_meta_embed.shape),
            list(self._meta_timestamps.shape),
        )
