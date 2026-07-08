from omegaconf import OmegaConf


def register_resolvers():
    if not OmegaConf.has_resolver("not"):
        OmegaConf.register_new_resolver("not", lambda boolean: not boolean)

    if not OmegaConf.has_resolver("equal"):
        OmegaConf.register_new_resolver("equal", lambda arg1, arg2: arg1 == arg2)

    def conditional_resolver(condition, arg1, arg2):
        return arg1 if condition else arg2

    if not OmegaConf.has_resolver("ifelse"):
        OmegaConf.register_new_resolver("ifelse", conditional_resolver)
