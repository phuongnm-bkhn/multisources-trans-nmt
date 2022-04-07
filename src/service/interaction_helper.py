from fairseq_interactive import *
from translation_connector import TranslationConnector

def encode_fn(x, bpe, tokenizer):
    if tokenizer is not None:
        x = tokenizer.encode(x)
    if bpe is not None:
        x = bpe.encode(x)
    return x

def decode_fn(x, bpe, tokenizer):
    if bpe is not None:
        x = bpe.decode(x)
    if tokenizer is not None:
        x = tokenizer.decode(x)
    return x


def cus_make_batches(lines, args, task, max_positions, encode_fn, bpe, tokenizer, translation_connector):
    def encode_fn_target(x):
        return encode_fn(x, bpe, tokenizer)

    if args.constraints:
        # Strip (tab-delimited) contraints, if present, from input lines,
        # store them in batch_constraints
        batch_constraints = [list() for _ in lines]
        for i, line in enumerate(lines):
            if "\t" in line:
                lines[i], *batch_constraints[i] = line.split("\t")

        # Convert each List[str] to List[Tensor]
        for i, constraint_list in enumerate(batch_constraints):
            batch_constraints[i] = [
                task.target_dictionary.encode_line(
                    encode_fn_target(constraint),
                    append_eos=False,
                    add_if_not_exist=False,
                )
                for constraint in constraint_list
            ]

    tokens = [
        task.source_dictionary.encode_line(
            encode_fn(src_str, bpe, tokenizer), add_if_not_exist=False
        ).long()
        for src_str in lines
    ]

    template_arr = [translation_connector.get_translation(
                s_in=encode_fn(src_str, bpe, tokenizer),
                source_lang=args.source_lang,
                target_lang=args.template_type,
                model_id="transformer",
            )
        for src_str in lines
    ]
    template_tokens = [
        task.template_dictionary.encode_line(
            templ_str, add_if_not_exist=False
        ).long()
        for templ_str in template_arr
    ]
    templ_lengths = [t.numel() for t in template_tokens]

    if args.constraints:
        constraints_tensor = pack_constraints(batch_constraints)
    else:
        constraints_tensor = None

    lengths = [t.numel() for t in tokens]
    itr = task.get_batch_iterator(
        dataset=task.build_dataset_for_inference(
            tokens, lengths, 
            template_tokens=template_tokens,
            template_tokens_sizes=templ_lengths,
            constraints=constraints_tensor,
        ),
        max_tokens=args.max_tokens,
        max_sentences=args.batch_size,
        max_positions=max_positions,
        ignore_invalid_inputs=args.skip_invalid_size_inputs_valid_test,
    ).next_epoch_itr(shuffle=False)
    for batch in itr:
        ids = batch["id"]
        src_tokens = batch["net_input"]["src_tokens"]
        src_lengths = batch["net_input"]["src_lengths"]
        templ_tokens = batch["net_input"]["template_tokens"]
        templ_lengths = torch.LongTensor([s.numel() for s in batch["net_input"]["template_tokens"]])

        constraints = batch.get("constraints", None)

        yield Batch(
            ids=ids,
            src_tokens=src_tokens,
            src_lengths=src_lengths,
            templ_tokens=templ_tokens,
            templ_lengths=templ_lengths,
            constraints=constraints,
        )


class InteractionHelper:
    def __init__(self, input_args) -> None:

        parser = options.get_interactive_generation_parser()
        args = options.parse_args_and_arch(parser, input_args=input_args)
        

        utils.import_user_module(args)

        if args.buffer_size < 1:
            args.buffer_size = 1
        if args.max_tokens is None and args.batch_size is None:
            args.batch_size = 1

        assert (
            not args.sampling or args.nbest == args.beam
        ), "--sampling requires --nbest to be equal to --beam"
        assert (
            not args.batch_size or args.batch_size <= args.buffer_size
        ), "--batch-size cannot be larger than --buffer-size"

        logger.info(args)

        # Fix seed for stochastic decoding
        if args.seed is not None and not args.no_seed_provided:
            np.random.seed(args.seed)
            utils.set_torch_seed(args.seed)

        use_cuda = torch.cuda.is_available() and not args.cpu

        # Setup task, e.g., translation
        task = tasks.setup_task(args)

        # Load ensemble
        logger.info("loading model(s) from {}".format(args.path))
        models, _model_args = checkpoint_utils.load_model_ensemble(
            args.path.split(os.pathsep),
            arg_overrides=eval(args.model_overrides),
            task=task,
            suffix=getattr(args, "checkpoint_suffix", ""),
            strict=(args.checkpoint_shard_count == 1),
            num_shards=args.checkpoint_shard_count,
        )

        # Optimize ensemble for generation
        for model in models:
            if args.fp16:
                model.half()
            if use_cuda and not args.pipeline_model_parallel:
                model.cuda()
            model.prepare_for_inference_(args)

        # Initialize generator
        self.generator = task.build_generator(models, args)

        # Handle tokenization and BPE
        tokenizer = encoders.build_tokenizer(args)
        bpe = encoders.build_bpe(args)


        # Load alignment dictionary for unknown word replacement
        # (None if no unknown word replacement, empty if no path to align dictionary)
        self.align_dict = utils.load_align_dict(args.replace_unk)

        max_positions = utils.resolve_max_positions(
            task.max_positions(), *[model.max_positions() for model in models]
        )

        if args.constraints:
            logger.warning(
                "NOTE: Constrained decoding currently assumes a shared subword vocabulary."
            )

        if args.buffer_size > 1:
            logger.info("Sentence buffer size: %s", args.buffer_size)
        logger.info("NOTE: hypothesis and token scores are output in base 2")
        
        self.start_id = 0
        self.args = args
        self.task = task
        self.max_positions = max_positions
        self.translation_connector = TranslationConnector()
        self.bpe = bpe
        self.tokenizer = tokenizer
        self.use_cuda = use_cuda
        self.models = models

    def translate(self, input_str):
        start_time  = time.time()
        total_translate_time = 0

        results = []
        templ_return = []
        inputs = [input_str]
        for batch in cus_make_batches(inputs, self.args, self.task, self.max_positions, encode_fn, \
                self.bpe, self.tokenizer,  self.translation_connector):
            bsz = batch.src_tokens.size(0)
            src_tokens = batch.src_tokens
            src_lengths = batch.src_lengths
            templ_tokens = batch.templ_tokens
            templ_lengths = batch.templ_lengths
            constraints = batch.constraints
            if self.use_cuda:
                src_tokens = src_tokens.cuda()
                src_lengths = src_lengths.cuda()
                templ_tokens = templ_tokens.cuda()
                templ_lengths = templ_lengths.cuda()
                if constraints is not None:
                    constraints = constraints.cuda()
            
            templ_return = templ_return + [self.task.template_dict.string(templ_tokens[i], self.args.remove_bpe) 
                                            for i in range(templ_tokens.shape[0])]

            sample = {
                "net_input": {
                    "src_tokens": src_tokens,
                    "src_lengths": src_lengths,
                    "template_tokens": templ_tokens,
                    # "template_lengths": templ_lengths,
                },
            }
            translate_start_time = time.time()
            translations = self.task.inference_step(
                self.generator, self.models, sample, constraints=constraints
            )
            translate_time = time.time() - translate_start_time
            total_translate_time += translate_time
            list_constraints = [[] for _ in range(bsz)]
            if self.args.constraints:
                list_constraints = [unpack_constraints(c) for c in constraints]
            for i, (id, hypos) in enumerate(zip(batch.ids.tolist(), translations)):
                src_tokens_i = utils.strip_pad(src_tokens[i], self.task.tgt_dict.pad())
                constraints = list_constraints[i]
                results.append(
                    (
                        self.start_id + id,
                        src_tokens_i,
                        hypos,
                        {
                            "constraints": constraints,
                            "time": translate_time / len(translations),
                        },
                    )
                )

        # sort output to match input order
        return_result = []
        for id_, src_tokens, hypos, info in sorted(results, key=lambda x: x[0]):
            if self.task.src_dict is not None:
                src_str = self.task.src_dict.string(src_tokens, self.args.remove_bpe)
                print("S-{}\t{}".format(id_, src_str))
                print("W-{}\t{:.3f}\tseconds".format(id_, info["time"]))
                for constraint in info["constraints"]:
                    print(
                        "C-{}\t{}".format(
                            id_, self.task.tgt_dict.string(constraint, self.args.remove_bpe)
                        )
                    )

            # Process top predictions
            for hypo in hypos[: min(len(hypos), self.args.nbest)]:
                hypo_tokens, hypo_str, alignment = utils.post_process_prediction(
                    hypo_tokens=hypo["tokens"].int().cpu(),
                    src_str=src_str,
                    alignment=hypo["alignment"],
                    align_dict=self.align_dict,
                    tgt_dict=self.task.tgt_dict,
                    remove_bpe=self.args.remove_bpe,
                    extra_symbols_to_ignore=get_symbols_to_strip_from_output(self.generator),
                )
                detok_hypo_str = decode_fn(hypo_str, self.bpe, self.tokenizer)
                score = hypo["score"] / math.log(2)  # convert to base 2
                # original hypothesis (after tokenization and BPE)
                print("H-{}\t{}\t{}".format(id_, score, hypo_str))
                # detokenized hypothesis
                print("D-{}\t{}\t{}".format(id_, score, detok_hypo_str))

                if hypo == hypos[0]:
                    return_result.append(detok_hypo_str)

                print(
                    "P-{}\t{}".format(
                        id_,
                        " ".join(
                            map(
                                lambda x: "{:.4f}".format(x),
                                # convert from base e to base 2
                                hypo["positional_scores"].div_(math.log(2)).tolist(),
                            )
                        ),
                    )
                )
                if self.args.print_alignment:
                    alignment_str = " ".join(
                        ["{}-{}".format(src, tgt) for src, tgt in alignment]
                    )
                    print("A-{}\t{}".format(id_, alignment_str))

            
        # update running id_ counter
        self.start_id += len(inputs)

        logger.info(
            "Total time: {:.3f} seconds; translation time: {:.3f}".format(
                time.time() - start_time, total_translate_time
            )
        )
        return " ".join(return_result), { "template": templ_return }
